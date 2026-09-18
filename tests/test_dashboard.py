import os
import tempfile
import json
import time
from unittest.mock import MagicMock, patch
from io import BytesIO

from src.dashboard.server import (
    BotProcessManager,
    DashboardRequestHandler,
)
from src.config.settings import BotConfig, GeneralConfig
from src.storage.state_store import StateStore
from src.collectors.orchestrator import CollectionEvaluator
from tests.test_acceptance import make_realistic_passing_collection
from tests.mock_data import make_sample_sales_events, make_sample_floor_history
from src.models.collection import CollectionStats, Offer

def test_bot_process_manager_no_pid():
    with patch("os.path.exists", return_value=False):
        assert BotProcessManager.get_running_pid() is None

def test_bot_process_manager_start_and_stop():
    with patch("subprocess.Popen") as mock_popen, \
         patch("builtins.open", MagicMock()), \
         patch("os.path.exists", side_effect=lambda p: True if "bot.pid" in p else False), \
         patch("os.kill") as mock_kill, \
         patch("os.remove") as mock_remove:

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        # Start bot
        with patch.object(BotProcessManager, "get_running_pid", return_value=None):
            res = BotProcessManager.start_bot()
            assert res["success"] is True
            assert res["pid"] == 99999

        # Stop bot
        with patch.object(BotProcessManager, "get_running_pid", return_value=99999):
            res_stop = BotProcessManager.stop_bot()
            assert res_stop["success"] is True
            assert res_stop["status"] == "STOPPED"

class DummyRequest:
    def __init__(self, data=b""):
        self._data = data
    def makefile(self, *args, **kwargs):
        return BytesIO(self._data)

def make_dummy_handler(method: str, path: str, body: bytes = b"", config=None, state_store=None, evaluator=None):
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.headers = {"Content-Length": str(len(body))} if body else {}
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.config = config or BotConfig()
    handler.state_store = state_store or MagicMock()
    handler.evaluator = evaluator or MagicMock()
    handler.server = MagicMock()
    handler.close_connection = True
    handler.responses = {}

    def send_response(status, message=None):
        handler._status = status
    handler.send_response = send_response

    handler._headers = {}
    def send_header(k, v):
        handler._headers[k] = v
    handler.send_header = send_header

    def end_headers():
        pass
    handler.end_headers = end_headers

    return handler

def test_dashboard_api_status():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        store = StateStore(db_path)
        cfg = BotConfig(general=GeneralConfig(state_db_path=db_path, data_root=temp_dir))

        handler = make_dummy_handler("GET", "/api/status", config=cfg, state_store=store)
        handler.do_GET()

        assert handler._status == 200
        data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert "status" in data
        assert "is_running" in data
        assert "total_candidates" in data

def test_dashboard_api_candidates_and_dossier():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_root = os.path.join(temp_dir, "data")
        candidate_dir = os.path.join(data_root, "2026-09-19", "Test-Project")
        os.makedirs(candidate_dir, exist_ok=True)
        info_path = os.path.join(candidate_dir, "Info.md")
        with open(info_path, "w", encoding="utf-8") as f:
            f.write("# Project: Test Project\n\n## BOT Final Result\n\n**PASS**\n")

        db_path = os.path.join(temp_dir, "bot.db")
        store = StateStore(db_path)
        cfg = BotConfig(general=GeneralConfig(data_root=data_root, state_db_path=db_path))

        # 1. GET /api/candidates
        handler = make_dummy_handler("GET", "/api/candidates", config=cfg, state_store=store)
        handler.do_GET()
        assert handler._status == 200
        data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert data["count"] == 1
        assert data["candidates"][0]["project_name"] == "Test Project"

        # 2. GET /api/candidate/dossier
        handler2 = make_dummy_handler("GET", "/api/candidate/dossier?date=2026-09-19&project=Test-Project", config=cfg, state_store=store)
        handler2.do_GET()
        assert handler2._status == 200
        dossier = json.loads(handler2.wfile.getvalue().decode("utf-8"))
        assert "# Project: Test Project" in dossier["content"]

def test_dashboard_api_inspect():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "bot.db")
        store = StateStore(db_path)
        cfg = BotConfig(general=GeneralConfig(state_db_path=db_path, data_root=temp_dir))

        mock_provider = MagicMock()
        mock_provider.get_collection.return_value = make_realistic_passing_collection()
        mock_provider.get_active_listings_count.return_value = (250, False)
        mock_provider.get_sale_events.return_value = make_sample_sales_events([1, 1, 1, 1, 1, 1, 1])
        curr_floor, p1, p7 = make_sample_floor_history(1.25, 2.0, 3.0)
        mock_provider.get_collection_stats.return_value = CollectionStats(floor_price=curr_floor)
        mock_provider.get_floor_price_history.side_effect = lambda slug, timeframe="one_day": p1 if timeframe == "one_day" else p7
        mock_provider.get_top_offer.return_value = Offer(order_hash="0x1", chain="ethereum", price_value=1.1, price_currency="WETH")

        evaluator = CollectionEvaluator(mock_provider, store, cfg)

        handler = make_dummy_handler("GET", "/api/inspect?slug=solitary-voyagers", config=cfg, state_store=store, evaluator=evaluator)
        handler.do_GET()

        assert handler._status == 200
        data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert data["slug"] == "solitary-voyagers"
        assert data["evaluated"] is True
        assert data["is_overall_pass"] is True
        assert len(data["criteria"]) == 7
        assert "project_age" in data["criteria"]
        assert "trading_frequency" in data["criteria"]
        assert "listed_items" in data["criteria"]
