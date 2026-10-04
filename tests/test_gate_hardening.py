"""Deterministic business gates supplement model instructions and quoted evidence."""
import copy
import unittest

from qa.core import DIMENSIONS, InvalidEvaluation, canonicalize, evaluate_gate


def fixture():
    turns = canonicalize({'content': {'utterances': [
        {'participant_type': 'internal', 'user_id': 7,
         'text': 'Thanks for calling. How can I help with your lighting project?'},
        {'participant_type': 'external',
         'text': 'Can I use this connector with my LED strip?'},
        {'participant_type': 'internal', 'user_id': 7,
         'text': 'I can check the connector compatibility with our technical team.'},
    ]}})
    evidence = lambda turn: [{'turn_id': turn, 'quote': turns[turn - 1]['text']}]
    dimensions = {name: {'applicability': 'not_applicable', 'anchor': None,
                         'reason': 'No relevant opportunity in this synthetic call.', 'evidence': []}
                  for name in DIMENSIONS}
    for name, turn in (('rapport', 1), ('technical_escalation', 3), ('call_flow', 3)):
        dimensions[name] = {'applicability': 'applicable', 'anchor': 3,
                            'reason': 'Appropriate observable behavior.', 'evidence': evidence(turn)}
    result = {'call_type': 'service', 'context': 'Synthetic compatibility question routed to the technical team.',
              'dimensions': dimensions,
              'strengths': [{'theme': 'ownership', 'behavior': 'Offers technical verification.',
                             'evidence': evidence(3), 'next_action': 'Continue checking compatibility before confirming.'}],
              'improvements': [], 'review_reasons': []}
    accuracy = {'theme': 'accuracy_review', 'behavior': 'Compatibility still requires product verification.',
                'evidence': evidence(3), 'next_action': 'Ask an SME to verify connector compatibility.'}
    return result, turns, accuracy


class GateHardeningTests(unittest.TestCase):
    def test_accuracy_improvement_requires_review_even_without_model_review_reason(self):
        result, turns, accuracy = fixture()
        result['improvements'] = [accuracy]
        original = copy.deepcopy(result)
        gate = evaluate_gate(result, turns, '7')
        self.assertEqual(gate['status'], 'review')
        self.assertEqual(gate['total'], 75)
        self.assertIn('Technical accuracy requires subject-matter review', gate['review_reasons'])
        self.assertEqual(result, original, 'Gate must not rewrite the model assessment.')

    def test_accuracy_theme_in_strength_cannot_bypass_review(self):
        result, turns, accuracy = fixture()
        result['strengths'] = [accuracy]
        self.assertEqual(evaluate_gate(result, turns, '7')['status'], 'review')

    def test_accuracy_flags_preserve_existing_reasons_without_duplicates(self):
        result, turns, accuracy = fixture()
        result['strengths'].append(copy.deepcopy(accuracy))
        result['improvements'] = [accuracy]
        result['review_reasons'] = ['Partial conversation; confirm the outcome.']
        gate = evaluate_gate(result, turns, '7')
        self.assertEqual(gate['review_reasons'], [
            'Partial conversation; confirm the outcome.',
            'Technical accuracy requires subject-matter review'])

    def test_accuracy_flag_does_not_allow_fabricated_evidence(self):
        result, turns, accuracy = fixture()
        accuracy['evidence'][0]['quote'] = 'This connector is certified compatible.'
        result['improvements'] = [accuracy]
        with self.assertRaisesRegex(InvalidEvaluation, 'evidence_quote_not_in_source'):
            evaluate_gate(result, turns, '7')

    def test_more_than_three_evidenced_improvements_is_rejected(self):
        result, turns, _ = fixture()
        result['improvements'] = [copy.deepcopy(result['strengths'][0]) for _ in range(4)]
        with self.assertRaisesRegex(InvalidEvaluation, 'at_most_three_improvements_required'):
            evaluate_gate(result, turns, '7')

    def test_zero_through_three_non_accuracy_improvements_keep_normal_validation(self):
        for count in range(4):
            result, turns, _ = fixture()
            result['improvements'] = [copy.deepcopy(result['strengths'][0]) for _ in range(count)]
            with self.subTest(count=count):
                gate = evaluate_gate(result, turns, '7')
                self.assertEqual(gate, {'status': 'validated', 'total': 75, 'review_reasons': []})


if __name__ == '__main__':
    unittest.main()
