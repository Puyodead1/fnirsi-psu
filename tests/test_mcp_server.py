"""End-to-end test of the MCP extension server over stdio (simulated PSU).

Run:  python -m unittest discover -s tests      (needs pyserial installed)
Launches extension/server/main.py the way Claude Desktop does (config via env)
and drives it with JSON-RPC. Set FNIRSI_EXT_DIR to test a built/unpacked
bundle instead of the source tree.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.environ.get("FNIRSI_EXT_DIR") or os.path.join(ROOT, "extension")


class Client:
    def __init__(self, **env):
        e = dict(os.environ)
        e.update({"PYTHONPATH": os.path.join(EXT, "server", "lib"),
                  "PYTHONIOENCODING": "utf-8",
                  # mimic unsubstituted / empty user_config values
                  "FNIRSI_PORT": "", "FNIRSI_BAUD": "9600", "FNIRSI_ADDRESS": "1",
                  "FNIRSI_MAX_V": "0", "FNIRSI_MAX_I": "0", "FNIRSI_SIMULATE": "false"})
        e.update({k: str(v) for k, v in env.items()})
        self.p = subprocess.Popen([sys.executable, os.path.join(EXT, "server", "main.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=e)
        self.n = 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        if notify:
            return None
        resp = json.loads(self.p.stdout.readline())
        assert resp["id"] == self.n, resp
        return resp

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        return r["isError"], (text if r["isError"] else json.loads(text))

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)
        self.p.stdout.close()


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.c = Client(FNIRSI_SIMULATE="true", FNIRSI_MAX_V="12")
        r = self.c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.c.rpc("notifications/initialized", notify=True)

    def tearDown(self):
        self.c.close()

    def test_tools_list_matches_manifest(self):
        tools = {t["name"] for t in self.c.rpc("tools/list")["result"]["tools"]}
        with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
            declared = {t["name"] for t in json.load(f)["tools"]}
        self.assertEqual(tools, declared)

    def test_workflow(self):
        c = self.c
        err, st = c.call("connect")
        self.assertFalse(err, st)
        self.assertEqual(st["model"], "SIM-PSU")
        self.assertEqual(st["limits"]["voltage_v"], 12.0)      # user cap < device 30 V

        err, msg = c.call("set_voltage", voltage_v=15)
        self.assertTrue(err)
        self.assertIn("exceeds", msg)

        err, st = c.call("configure", voltage_v=5, current_a=1, output_on=True)
        self.assertFalse(err, st)
        self.assertTrue(st["output_on"])
        self.assertAlmostEqual(st["output"]["current_a"], 0.5, places=3)  # 5 V / 10 ohm
        self.assertEqual(st["mode"], "CV")

        err, st = c.call("set_current", current_a=0.2)
        err, m = c.call("measure")
        self.assertEqual(m["mode"], "CC")
        self.assertAlmostEqual(m["output"]["voltage_v"], 2.0, places=3)

        err, mon = c.call("monitor", duration_s=1, interval_s=0.25)
        self.assertFalse(err, mon)
        self.assertGreaterEqual(len(mon["samples"]), 3)

        err, sw = c.call("sweep", mode="voltage", start=1, stop=3, step=0.5,
                         hold=1, dwell_s=0.2)
        self.assertFalse(err, sw)
        self.assertEqual([p["set"] for p in sw["points"]], [1, 1.5, 2, 2.5, 3])
        self.assertAlmostEqual(sw["points"][-1]["a"], 0.3, places=3)
        err, m = c.call("measure")
        self.assertFalse(m["output_on"])                        # turned off after sweep

        err, pr = c.call("set_preset", preset=3, voltage_v=3.3, current_a=0.25)
        self.assertAlmostEqual(pr["voltage_v"], 3.3, places=3)
        err, ap = c.call("apply_preset", preset=3)
        self.assertFalse(err, ap)

        err, prot = c.call("set_protection", ovp_v=13, ocp_a=2)
        self.assertEqual(prot["ovp_v"], 13.0)

        err, raw = c.call("raw_command", cmd="A1", reg="C0")
        self.assertTrue(raw["reply_received"])
        self.assertEqual(raw["sent"], "f1 a1 c0 01 00 c1")

        err, d = c.call("disconnect")
        self.assertEqual(d["disconnected"], "SIMULATED")

    def test_errors(self):
        err, msg = self.c.call("set_voltage", voltage_v="abc")
        self.assertTrue(err)
        r = self.c.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertIn("error", r)
        r = self.c.rpc("bogus/method")
        self.assertEqual(r["error"]["code"], -32601)

    def test_no_port_configured(self):
        c = Client()
        c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}})
        err, msg = c.call("measure")
        self.assertTrue(err)
        self.assertIn("Not connected", msg)
        err, ports = c.call("list_serial_ports")
        self.assertFalse(err)
        self.assertIn("ports", ports)
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
