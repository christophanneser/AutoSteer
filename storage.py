# Copyright 2022 Intel Corporation
# SPDX-License-Identifier: MIT
#
"""This module implements the connection to the SQLite3 database persisting all benchmarking data generated by AutoSteer"""
import json
import numpy as np
import pandas as pd
import random
import socket
import sys
import os
from datetime import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.sql import text
from sqlalchemy.exc import IntegrityError
import unittest

from utils.custom_logging import logger
from utils.util import read_sql_file

SCHEMA_FILE = 'schema.sql'
ENGINE = None
TESTED_DATABASE = None
BENCHMARK_ID = None


def _db():
    global ENGINE
    url = f'sqlite:///results/{TESTED_DATABASE}.sqlite'
    logger.debug('Connect to database: %s', url)
    ENGINE = create_engine(url)

    @event.listens_for(ENGINE, 'connect')
    def connect(dbapi_conn, _):
        """Load SQLite extension for median calculation"""
        extension_path = './sqlean-extensions/stats.so'

        if not os.path.isfile(extension_path):
            logger.fatal('Please, first download the required sqlite3 extension using sqlean-extensions/download.sh')
            sys.exit(1)

        dbapi_conn.enable_load_extension(True)
        dbapi_conn.load_extension(extension_path)
        dbapi_conn.enable_load_extension(False)

    conn = ENGINE.connect()
    schema = read_sql_file(SCHEMA_FILE)

    for statement in schema.split(';'):
        if len(statement.strip()) > 0:
            try:
                conn.execute(statement)
            except Exception as e:
                print(e)
                raise e
    return conn


def register_benchmark(name: str) -> int:
    # Register a new benchmark and return its id
    with _db() as conn:
        try:
            stmt = text('INSERT INTO benchmarks (name) VALUES (:name)')
            conn.execute(stmt, name=name)
        except IntegrityError:
            pass
        return conn.execute('SELECT benchmarks.id FROM benchmarks WHERE name=:name', name=name).fetchone()[0]


def register_query(query_path):
    # Register a new query
    with _db() as conn:
        try:
            stmt = text('INSERT INTO queries (benchmark_id, query_path, result_fingerprint) VALUES (:benchmark_id, :query_path, :result_fingerprint )')
            conn.execute(stmt, benchmark_id=BENCHMARK_ID, query_path=query_path, result_fingerprint=None)
        except IntegrityError:
            pass


def register_query_fingerprint(query_path, fingerprint):
    with _db() as conn:
        result = conn.execute(text('SELECT result_fingerprint FROM queries WHERE query_path= :query_path'), query_path=query_path).fetchone()[0]
        if result is None:
            conn.execute(text('UPDATE queries SET result_fingerprint = :fingerprint WHERE query_path = :query_path;'),
                         fingerprint=fingerprint, query_path=query_path)
            return True
        elif result != fingerprint:
            return False  # fingerprints do not match
        return True


def register_optimizer(query_path, optimizer, required: bool):
    with _db() as conn:
        try:
            table = 'query_effective_optimizers' if not required else 'query_required_optimizers'
            stmt = text(f'INSERT INTO {table} (query_id, optimizer) '
                        'SELECT id, :optimizer FROM queries WHERE query_path = :query_path')
            conn.execute(stmt, table=table, optimizer=optimizer, query_path=query_path)
        except IntegrityError:
            pass  # do not store duplicates


def register_optimizer_dependency(query_path, optimizer, dependency):
    with _db() as conn:
        try:
            stmt = text('INSERT INTO query_effective_optimizers_dependencies (query_id, optimizer, dependent_optimizer) '
                        'SELECT id, :optimizer, :dependency FROM queries WHERE query_path = :query_path')
            conn.execute(stmt, optimizer=optimizer, dependency=dependency, query_path=query_path)
        except IntegrityError:
            pass  # do not store duplicates


class Measurement:
    """This class stores the measurement for a certain query and optimizer configuration"""

    def __init__(self, query_path, query_id, optimizer_config, disabled_rules, num_disabled_rules, plan_json, walltime):
        self.query_path = query_path
        self.query_id = query_id
        self.optimizer_config = optimizer_config
        self.disabled_rules = disabled_rules
        self.num_disabled_rules = num_disabled_rules
        self.plan_json = json.loads(plan_json)
        self.walltime = walltime


def experience(benchmark=None, training_ratio=0.8):
    """Get experience to train a neural network"""
    stmt = """SELECT qu.query_path, q.query_id, q.id,  q.disabled_rules, q.num_disabled_rules, q.query_plan, median(walltime)
            FROM measurements m, query_optimizer_configs q, queries qu
            WHERE m.query_optimizer_config_id = q.id
              AND q.query_plan != 'None' 
              AND qu.id = q.query_id
              AND qu.query_path like :benchmark
            group by qu.query_path, q.query_id, q.id, q.disabled_rules, q.num_disabled_rules, q.query_plan"""

    with _db() as conn:
        benchmark = '%%' if benchmark is None else '%%' + benchmark + '%%'
        df = pd.read_sql(stmt, conn, params={'benchmark': benchmark})
    rows = [Measurement(*row) for index, row in df.iterrows()]

    # Group training and test data by query
    result = {}
    for row in rows:
        if row.query_id in result:
            result[row.query_id].append(row)
        else:
            result[row.query_id] = [row]

    keys = list(result.keys())
    random.shuffle(keys)
    split_index = int(len(keys) * training_ratio)
    train_keys = keys[:split_index]
    test_keys = keys[split_index:]

    train_data = np.concatenate([result[key] for key in train_keys])
    test_data = np.concatenate([result[key] for key in test_keys])

    return train_data, test_data


def _get_optimizers(table_name, query_path, projections):
    """No SQL injections as this is a private function only called from within *this* module"""
    with _db() as conn:
        stmt = f"""
               SELECT {','.join(projections)}
               FROM queries q, {table_name} qro
               WHERE q.query_path=:query_path AND q.id = qro.query_id AND optimizer != ''
               """
        cursor = conn.execute(stmt, query_path=query_path)
        return cursor.fetchall()


def get_required_optimizers(query_path):
    return list(map(lambda res: res[0], _get_optimizers('query_required_optimizers', query_path, ['optimizer'])))


def get_effective_optimizers(query_path):
    return list(map(lambda res: res[0], _get_optimizers('query_effective_optimizers', query_path, ['optimizer'])))


def get_effective_optimizers_depedencies(query_path):
    return list(map(lambda res: [res[0], res[1]], _get_optimizers('query_effective_optimizers_dependencies', query_path, ['optimizer', 'dependent_optimizer'])))


def get_df(query, params):
    with _db() as conn:
        df = pd.read_sql(query, conn, params=params)
        return df


def select_query(query, params):
    with _db() as conn:
        cursor = conn.execute(query, *params)
        return [row[0] for row in cursor.fetchall()]


def register_query_config(query_path, disabled_rules, query_plan: dict, plan_hash):
    """
    Store the passed query optimizer configuration in the database.
    :returns: query plan is already known and a duplicate
    """
    check_for_duplicated_plans = """SELECT count(*)
        FROM queries q, query_optimizer_configs qoc
        WHERE q.id = qoc.query_id
              AND q.query_path = :query_path
              AND qoc.hash = :plan_hash
              AND qoc.disabled_rules != :disabled_rules"""
    result = select_query(check_for_duplicated_plans, {query_path: query_path, plan_hash: plan_hash, disabled_rules: disabled_rules})
    is_duplicate = result[0] > 0

    with _db() as conn:
        try:
            num_disabled_rules = 0 if disabled_rules is None else disabled_rules.count(',') + 1
            stmt = f"""INSERT INTO query_optimizer_configs
                   (query_id, disabled_rules, query_plan, num_disabled_rules, hash, duplicated_plan) 
                   SELECT id, :disabled_rules, :query_plan_processed , :num_disabled_rules, :plan_hash, :is_duplicate FROM queries WHERE query_path = '{query_path}'
                   """
            conn.execute(stmt, disabled_rules=str(disabled_rules), query_plan_processed=query_plan, num_disabled_rules=num_disabled_rules,
                         plan_hash=plan_hash, is_duplicate=is_duplicate)
        except IntegrityError:
            pass  # OK! Query configuration has already been inserted

    return is_duplicate


def check_for_existing_measurements(query_path, disabled_rules):
    query = """SELECT count(*) as num_measurements
                FROM measurements m, query_optimizer_configs qoc, queries q
                WHERE m.query_optimizer_config_id = qoc.id
                AND qoc.query_id = q.id
                AND q.query_path = :query_path
                AND qoc.disabled_rules = :disabled_rules
             """
    df = get_df(query, {'query_path': query_path, 'disabled_rules': disabled_rules})
    values = df['num_measurements']
    return values[0] > 0


def register_measurement(query_path, disabled_rules, walltime, input_data_size, nodes):
    logger.info('Serialize a new measurement for query %s and the disabled knobs [%s]', query_path, disabled_rules)
    with _db() as conn:
        now = datetime.now()
        query = """
                INSERT INTO measurements (query_optimizer_config_id, walltime, machine, time, input_data_size, num_compute_nodes)
                SELECT id, :walltime, :host, :time, :input_data_size, :nodes FROM query_optimizer_configs 
                WHERE query_id = (SELECT id FROM queries WHERE query_path = :query_path) AND disabled_rules = :disabled_rules 
                """
        conn.execute(query, walltime=walltime, host=socket.gethostname(), time=now.strftime('%m/%d/%y, %h:%m:%s'), input_data_size=input_data_size, nodes=nodes,
                     query_path=query_path, disabled_rules=str(disabled_rules))


def median_runtimes():
    class OptimizerConfigResult:
        def __init__(self, path, num_disabled_rules, disabled_rules, json_plan, runtime):
            self.path = path
            self.num_disabled_rules = num_disabled_rules
            self.disabled_rules = disabled_rules
            self.json_plan = json_plan
            self.runtime = runtime

    with _db() as conn:
        default_plans_stmt = """SELECT q.query_path, qoc.num_disabled_rules, qoc.disabled_rules, logical_plan_json, elapsed
        FROM queries q,  query_optimizer_configs qoc, measurements m
        WHERE q.id = qoc.query_id AND qoc.id = m.query_optimizer_config_id
        """
        df = pd.read_sql(default_plans_stmt, conn)
        default_median_runtimes = df.groupby(['query_path', 'num_disabled_rules', 'disabled_rules', 'logical_plan_json'])['elapsed'].median().reset_index()

        return [OptimizerConfigResult(*row) for index, row in default_median_runtimes.iterrows()]


def best_alternative_configuration(benchmark=None):
    class OptimizerConfigResult:
        def __init__(self, path, num_disabled_rules, runtime, runtime_baseline, savings, disabled_rules, rank):
            self.path = path
            self.num_disabled_rules = num_disabled_rules
            self.runtime = runtime
            self.runtime_baseline = runtime_baseline
            self.savings = savings
            self.disabled_rules = disabled_rules
            self.rank = rank

    stmt = read_sql_file('best_alternative_queries.sql')

    with _db() as conn:
        cursor = conn.execute(stmt, path=benchmark)
        return [OptimizerConfigResult(*row) for row in cursor.fetchall()]


class TestStorage(unittest.TestCase):
    """Test the storage class"""

    def test_median(self):
        with _db() as db:
            result = db.execute('SELECT MEDIAN(a) FROM (SELECT 1 AS a) AS tab').fetchall()
            assert len(result) == 1

    def test_queries(self):
        with _db() as db:
            result = db.execute('SELECT * FROM queries').fetchall()
            print(result)

    def test_optimizers(self):
        with _db() as db:
            result = db.execute('SELECT * FROM query_effective_optimizers;')
            print(result.fetchall())
