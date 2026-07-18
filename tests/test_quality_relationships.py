import unittest

import quality_report


class RelationshipQualityTest(unittest.TestCase):
    def test_high_scoring_weak_edge_is_not_hidden_by_person_scale(self):
        index = {
            "edges": [
                {
                    "kind": "person_person",
                    "relation_type": "business_handoff",
                    "score": 3000,
                },
                {
                    "kind": "person_project",
                    "relation_type": "co_mention_weak",
                    "score": 100,
                },
            ],
            "summary": {},
        }

        issues, _, _ = quality_report.check_relationships(index)

        self.assertIn(
            "weak_project_edge_high_score",
            {issue["id"] for issue in issues},
        )


if __name__ == "__main__":
    unittest.main()
