"""Run queue/worker invariants against disposable schemas in real PostgreSQL."""
import os
import tempfile
import unittest
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from test_quality import QueueTests, WorkerTests
from qa.db import Store

class PostgresFixture:
    def setUp(self):
        import psycopg
        from psycopg import sql
        self.tmp=tempfile.TemporaryDirectory()
        self.schema='qa_test_'+uuid.uuid4().hex
        self.base_url=os.environ['QA_TEST_DATABASE_URL']
        with psycopg.connect(self.base_url) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        parts=urlsplit(self.base_url)
        query=dict(parse_qsl(parts.query));query['options']='-c search_path='+self.schema
        self.store=Store(urlunsplit(parts._replace(query=urlencode(query))))
    def tearDown(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.base_url) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.tmp.cleanup()

@unittest.skipUnless(os.environ.get('QA_TEST_DATABASE_URL'),'PostgreSQL URL not set')
class PostgresQueueTests(PostgresFixture,QueueTests): pass

@unittest.skipUnless(os.environ.get('QA_TEST_DATABASE_URL'),'PostgreSQL URL not set')
class PostgresWorkerTests(PostgresFixture,WorkerTests): pass

# Imported test classes should run only from their own source module.
del QueueTests, WorkerTests
