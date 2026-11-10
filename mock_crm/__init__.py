"""A small fake CRM REST API used as the pipeline's data source.

Everything is synthetic (Faker with a fixed seed), so two runs with the same
config serve byte-identical data.
"""

from mock_crm.app import create_app
from mock_crm.config import MockConfig
from mock_crm.data import CRMStore

__all__ = ["CRMStore", "MockConfig", "create_app"]
