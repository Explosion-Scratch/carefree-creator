import os
import json
import time
import yaml
import redis
import datetime
import requests
import logging.config

from enum import Enum
from kafka import KafkaProducer
from kafka import KafkaAdminClient
from typing import Any
from typing import Dict
from typing import Optional
from typing import NamedTuple
from fastapi import FastAPI
from pydantic import BaseModel
from qcloud_cos import CosConfig
from qcloud_cos import CosS3Client
from pkg_resources import get_distribution
from cftool.misc import random_hash
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware

from cfclient.utils import get_responses

from cfcreator import *


app = FastAPI()
root = os.path.dirname(__file__)

origins = [
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# logging
logging_root = os.path.join(root, "logs", "producer")
os.makedirs(logging_root, exist_ok=True)
with open(os.path.join(root, "config.yml")) as f:
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S-%f")
    log_path = os.path.join(logging_root, f"{timestamp}.log")
    config = yaml.load(f, Loader=yaml.FullLoader)
    config["handlers"]["file"]["filename"] = log_path
    logging.config.dictConfig(config)

excluded_endpoints = {"/health", "/redoc", "/docs", "/openapi.json"}

class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args:
            return False
        if len(record.args) < 3:
            return False
        if record.args[2] in excluded_endpoints:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
logging.getLogger("dicttoxml").disabled = True
logging.getLogger("kafka.conn").disabled = True
logging.getLogger("kafka.cluster").disabled = True
logging.getLogger("kafka.coordinator").disabled = True
logging.getLogger("kafka.consumer.subscription_state").disabled = True


# clients
config = CosConfig(Region=REGION, SecretId=SECRET_ID, SecretKey=SECRET_KEY)
cos_client = CosS3Client(config)
redis_client = redis.Redis(host="localhost", port=6379, db=0)
kafka_admin = KafkaAdminClient(bootstrap_servers="172.17.16.8:9092")
kafka_producer = KafkaProducer(bootstrap_servers="172.17.16.8:9092")


# schema


DOCS_TITLE = "FastAPI client"
DOCS_VERSION = get_distribution("carefree-client").version
DOCS_DESCRIPTION = (
    "This is a client framework based on FastAPI. "
    "It also supports interacting with Triton Inference Server."
)

def carefree_schema() -> Dict[str, Any]:
    schema = get_openapi(
        title=DOCS_TITLE,
        version=DOCS_VERSION,
        description=DOCS_DESCRIPTION,
        contact={
            "name": "Get Help with this API",
            "email": "syameimaru.saki@gmail.com",
        },
        routes=app.routes,
    )
    app.openapi_schema = schema
    return app.openapi_schema


# health check


class HealthStatus(Enum):
    ALIVE = "alive"

class HealthCheckResponse(BaseModel):
    status: HealthStatus

@app.get("/health", response_model=HealthCheckResponse)
async def health_check() -> HealthCheckResponse:
    return {"status": "alive"}


# get prompt


@app.post("/translate")
@app.post("/get_prompt")
def get_prompt(data: GetPromptModel) -> GetPromptResponse:
    text = data.text
    audit = audit_text(cos_client, text)
    if not audit.safe:
        return GetPromptResponse(text="", success=False, reason=audit.reason)
    return GetPromptResponse(text=text, success=True, reason="")


# kafka & redis


pending_queue_key = "KAFKA_PENDING_QUEUE"


def get_redis_number(key: str) -> Optional[int]:
    data = redis_client.get(key)
    if data is None:
        return None
    return int(data.decode())  # type: ignore


class ProducerModel(BaseModel):
    task: str
    params: Dict[str, Any]


class ProducerResponseModel(BaseModel):
    uid: str


@app.post("/push/{topic}", responses=get_responses(ProducerResponseModel))
async def push(data: ProducerModel, topic: str) -> ProducerResponseModel:
    uid = random_hash()
    kafka_producer.send(
        topic,
        json.dumps(
            dict(
                uid=uid,
                task=data.task,
                params=data.params,
            ),
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    queue = get_pending_queue()
    queue.append(uid)
    redis_client.set(pending_queue_key, json.dumps(queue))
    redis_client.set(uid, json.dumps(dict(status="pending", data=None)))
    return ProducerResponseModel(uid=uid)


class ServerStatusModel(BaseModel):
    is_ready: bool
    num_pending: int


def get_pending_queue() -> list:
    data = redis_client.get(pending_queue_key)
    if data is None:
        return []
    return json.loads(data)


@app.get("/server_status", responses=get_responses(ServerStatusModel))
async def server_status() -> ServerStatusModel:
    members = kafka_admin.describe_consumer_groups(["creator-consumer-1"])[0].members
    return ServerStatusModel(is_ready=len(members) > 0, num_pending=len(get_pending_queue()))


class Status(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    FINISHED = "finished"
    EXCEPTION = "exception"


class StatusModel(BaseModel):
    status: Status
    pending: int
    data: Optional[Any]


class StatusData(NamedTuple):
    status: Status
    data: Optional[Any]


def fetch_redis(uid: str) -> StatusData:
    data = redis_client.get(uid)
    if data is None:
        return StatusData(status="pending", data=None)
    return StatusData(**json.loads(data))


@app.get("/status/{uid}", responses=get_responses(StatusModel))
async def get_status(uid: str) -> StatusModel:
    record = fetch_redis(uid)
    if record.status == "finished":
        lag = 0
    else:
        queue = get_pending_queue()
        pop_indices = []
        for i, i_uid in enumerate(queue):
            if fetch_redis(i_uid).status == "finished":
                pop_indices.append(i)
        for idx in pop_indices[::-1]:
            queue.pop(idx)
        if pop_indices:
            redis_client.set(pending_queue_key, json.dumps(queue))
        try:
            lag = queue.index(uid)
        except:
            lag = len(queue) - 1
    return StatusModel(status=record.status, data=record.data, pending=lag)


# events


@app.on_event("startup")
async def startup() -> None:
    pass

@app.on_event("shutdown")
async def shutdown() -> None:
    pass


# schema

app.openapi = carefree_schema


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("interface:app", host="0.0.0.0", port=8989, reload=True)
