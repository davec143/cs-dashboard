"""Run queue/worker invariants against disposable schemas in real PostgreSQL."""
import os
import tempfile
import unittest
import uuid
from urllib.parse import parse_qsl, urlencode, quote, urlsplit, urlunsplit
from test_quality import QueueTests, WorkerTests
from qa.db import Store
from test_web import WebTests

class PostgresFixture:
    def pg_store(self):
        import psycopg
        from psycopg import sql
        self.schema='qa_test_'+uuid.uuid4().hex
        self.base_url=os.environ['QA_TEST_DATABASE_URL']
        with psycopg.connect(self.base_url) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        parts=urlsplit(self.base_url)
        query=dict(parse_qsl(parts.query));query['options']='-c search_path='+self.schema
        return Store(urlunsplit(parts._replace(query=urlencode(query,quote_via=quote))))
    def drop_schema(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.base_url) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
    def setUp(self):
        self.store=self.pg_store()
    def tearDown(self):
        pass

@unittest.skipUnless(os.environ.get('QA_TEST_DATABASE_URL'),'PostgreSQL URL not set')
class PostgresQueueTests(PostgresFixture,QueueTests): pass

@unittest.skipUnless(os.environ.get('QA_TEST_DATABASE_URL'),'PostgreSQL URL not set')
class PostgresWorkerTests(PostgresFixture,WorkerTests): pass

# Imported test classes should run only from their own source module.
del QueueTests, WorkerTests

@unittest.skipUnless(os.environ.get('QA_TEST_DATABASE_URL'),'PostgreSQL URL not set')
class PostgresWebTests(PostgresFixture, WebTests):
    setUp = WebTests.setUp
    tearDown = WebTests.tearDown
    make_store = PostgresFixture.pg_store

del WebTests
