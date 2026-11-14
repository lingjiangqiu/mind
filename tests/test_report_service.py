import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.entities import Base
from app.services.report import ReportService


class ReportServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.service = ReportService(self.db)

    def tearDown(self):
        self.db.close()

    def test_admin_collection_methods_are_bound_and_return_empty_lists(self):
        self.assertEqual(self.service.agent_run_traces(), [])
        self.assertEqual(self.service.tool_audits(), [])

    def test_missing_conversation_raises_domain_error(self):
        with self.assertRaisesRegex(ValueError, "Session not found"):
            self.service.conversation("missing")


if __name__ == "__main__":
    unittest.main()
