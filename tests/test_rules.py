import unittest

from src import rules
from src.domain import ConflictError, ValidationError


class RulesTest(unittest.TestCase):
    def test_diversion_grows_with_level(self):
        bridge = {"capacity": 100, "daily_vehicles": 1000, "daily_buses": 100}
        neighbor = {"capacity": 1000, "status": "normal"}
        light = rules.assess_capacity(bridge, "load_limit", neighbor, 10000, 1)
        heavy = rules.assess_capacity(bridge, "full_close", neighbor, 10000, 1)
        self.assertGreater(heavy["diverted_vehicles"], light["diverted_vehicles"])
        self.assertGreater(heavy["network_cost"], light["network_cost"])
        self.assertTrue(light["feasible"] and heavy["feasible"])

    def test_neighbor_closure_blocks_detour(self):
        bridge = {"capacity": 100, "daily_vehicles": 1000, "daily_buses": 100}
        neighbor = {"capacity": 1000, "status": "full_close"}
        result = rules.assess_capacity(bridge, "full_close", neighbor, 10000, 1)
        self.assertFalse(result["feasible"])
        self.assertTrue(any("绕行路径中断" in b for b in result["blockers"]))

    def test_neighbor_limit_reduces_free_capacity(self):
        neighbor = {"capacity": 1000, "status": "lane_close"}
        self.assertEqual(rules.neighbor_capacity(neighbor), 450.0)

    def test_network_budget_blocked(self):
        bridge = {"capacity": 100, "daily_vehicles": 1000, "daily_buses": 100}
        result = rules.assess_capacity(bridge, "full_close", None, 10, 0)
        self.assertFalse(result["feasible"])
        self.assertTrue(any("路网" in b for b in result["blockers"]))

    def test_transition_guards(self):
        self.assertTrue(rules.can_transition("pending_engineer", "pending_supervisor"))
        self.assertFalse(rules.can_transition("pending_engineer", "restricted"))
        with self.assertRaises(ConflictError):
            rules.validate_transition("restricted", "pending_supervisor")
        with self.assertRaises(ValidationError):
            rules.assess_capacity({"daily_vehicles": 1, "daily_buses": 1},
                                  "not-a-level", None, 10)

    def test_roles_for_transition(self):
        self.assertEqual(
            rules.roles_for_transition("pending_engineer", "pending_supervisor"),
            {"bridge_engineer"})
        self.assertEqual(
            rules.roles_for_transition("pending_supervisor", "restricted"),
            {"safety_supervisor"})
        self.assertIn(
            "safety_supervisor",
            rules.roles_for_transition("pending_engineer", "emergency_pending_review"))


if __name__ == "__main__":
    unittest.main()
