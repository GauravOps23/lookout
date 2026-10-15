"""A new view type = a new folder plus one line here."""
from app.views.api_response.plugin import ApiResponseView
from app.views.db_table.plugin import DbTableView
from app.views.ssh_health.plugin import SshHealthView

REGISTRY = {v.key: v for v in (SshHealthView(), DbTableView(), ApiResponseView())}
