"""Fast unit tests for the shared data-analysis scope guard."""

import unittest

from output_format import is_explicitly_out_of_scope


class OutputScopeTests(unittest.TestCase):
    def test_rejects_explicit_non_analysis_content(self):
        for question in (
            "Write Python code for factorial",
            "Show me this project's source code",
            "Tell me a joke",
            "What is the weather forecast?",
        ):
            with self.subTest(question=question):
                self.assertTrue(is_explicitly_out_of_scope(question))

    def test_allows_data_questions_even_when_search_terms_sound_technical(self):
        for question in (
            "How many users searched for Python books?",
            "Show journeys containing the keyword code",
            "Which app has the most searches?",
        ):
            with self.subTest(question=question):
                self.assertFalse(is_explicitly_out_of_scope(question))


if __name__ == "__main__":
    unittest.main()
