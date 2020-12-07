import asyncio
import json
import numpy as np
import pandas as pd
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster, dict_factory
from cassandra.concurrent import execute_concurrent_with_args

from .model import Model
from ..workers import main, report
from ...config import Config


# TODO:
#  - "models" table in Cassandra
#   - meeting_name, model_type, blob, details (trained on what data)
#  - REST endpoint to read list of all models
#  - add column for ML anomalies
#  - add REST endpoint for ML anomalies
class Worker:

    def __init__(
        self, call_info_table, roster_table, cassandra_session, config_consumer, job_consumer, producer
    ):
        self.call_info_table = call_info_table
        self.roster_table = roster_table
        self.session = cassandra_session
        self.session.row_factory = dict_factory
        self.config_consumer = config_consumer
        self.job_consumer = job_consumer
        self.kafka_producer = producer
        self.meeting_models = {}

        self.loop = None

    async def start(self):
        report('started')
        self.loop = asyncio.get_running_loop()
        await asyncio.gather(self.process_config(), self.process_jobs())

    async def process_config(self):
        async for msg in self.config_consumer:
            msg_dict = json.loads(msg.value)

            meeting_name = msg_dict['meeting_name']
            config_type = msg_dict['type']
            report(f'received configuration {config_type} for {meeting_name}')

            if config_type == 'schedule':
                model_id = msg_dict['model']
                self.load_model(meeting_name, model_id)
            elif config_type == 'unschedule':
                del self.meeting_models[meeting_name]
            else:
                # TODO: log unknown?
                pass

    async def process_jobs(self):
        async for msg in self.job_consumer:
            msg_dict = json.loads(msg.value)

            meeting_name = msg_dict['meeting_name']
            start, end = msg_dict['start'], msg_dict['end']
            report(f'received job for {meeting_name} - {start} to {end}')

            # TODO: we block here...
            model = self.meeting_models[meeting_name]
            ci_batch, roster_batch = self.load_data(meeting_name, start, end)
            ci_results = model.predict(ci_batch)
            roster_results = model.predict(roster_batch)
            self.save_anomalies(meeting_name, ci_results, roster_results)

            report(f'finished job for {meeting_name} - {start} to {end}')

    def load_model(self, meeting_name, model_id):
        result = self.session.execute(
            f'SELECT blob FROM models '
            f'WHERE model_id=%s;',
        (model_id, )).all()

        # TODO: handle incorrect model_id
        blob = result[0]['blob']
        model = Model(meeting_name)
        model.deserialize(blob)
        self.meeting_models[meeting_name] = model
        return model

    def load_data(self, meeting_name, start, end):
        ci_result = self.session.execute(
            f'SELECT * FROM call_info_update '
            f'WHERE meeting_name = %s AND datetime >= %s AND datetime <= %s;',
            (meeting_name, start, end)
        ).all()

        roster_result = self.session.execute(
            f'SELECT * FROM roster_update '
            f'WHERE meeting_name = %s AND datetime >= %s AND datetime <= %s;',
            (meeting_name, start, end)
        ).all()

        def extend(row_dict):
            return {k: row_dict.get(k, np.NaN) for k in Model.get_columns().keys()}
        ci_ext, roster_ext = list(map(extend, ci_result)), list(map(extend, roster_result))
        return pd.DataFrame(ci_ext, index=['datetime']), pd.DataFrame(roster_ext, index=['datetime'])

    def save_anomalies(self, meeting_name, ci_results, roster_results):
        ci_anomalies = [(meeting_name, timestamp) for timestamp, pred in ci_results.items() if pred]
        ci_stmt = self.session.prepare(
            f"UPDATE call_info_update "
            f"SET ml_anomaly=true "
            f"WHERE meeting_name=%s AND datetime=%s;"
        )
        execute_concurrent_with_args(self.session, ci_stmt, ci_anomalies)

        roster_anomalies = [(meeting_name, timestamp) for timestamp, pred in roster_results.items() if pred]
        roster_stmt = self.session.prepare(
            f"UPDATE roster_update "
            f"SET ml_anomaly=true "
            f"WHERE meeting_name=%s AND datetime=%s;"
        )
        execute_concurrent_with_args(self.session, roster_stmt, roster_anomalies)


async def setup(config: Config):
    producer = AIOKafkaProducer(bootstrap_servers=config.kafka_bootstrap_server)
    await producer.start()

    config_consumer = AIOKafkaConsumer('monitoring-anomalies-config', bootstrap_servers=config.kafka_bootstrap_server)
    job_consumer = AIOKafkaConsumer('monitoring-anomalies-jobs', bootstrap_servers=config.kafka_bootstrap_server)

    await config_consumer.start()
    await job_consumer.start()

    auth_provider = PlainTextAuthProvider(username=config.cassandra_user, password=config.cassandra_passwd)
    cassandra = Cluster([config.cassandra_host], port=config.cassandra_port, auth_provider=auth_provider)
    session = cassandra.connect(config.keyspace)

    return Worker(
        config.call_info_table, config.roster_table, session, config_consumer, job_consumer, producer
    )


async def teardown(worker: Worker):
    await worker.kafka_producer.stop()
    await worker.config_consumer.stop()
    await worker.job_consumer.stop()


if __name__ == '__main__':
    main(setup, teardown)
