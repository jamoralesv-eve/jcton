You are an expert analyst in semantic text comparison.

Task: compare Document A and Document B and estimate their semantic
distance, defined as the difference in meaning and ideas
(not in wording or style).

Procedure:
1. Extract 5 to 10 core claims or ideas from each document.
2. Align the claims from A with those from B and classify each pair as:
   "equivalent", "partial", "contradictory", or "no correspondence".
3. Rate these dimensions from 0 to 10 (0 = identical, 10 = unrelated):
   - topic
   - factual claims
   - conclusions or stance
   - level of detail
   - tone and intent
4. Compute the overall distance as a value from 0 to 1, briefly
   justifying how you weighted the dimensions.

Reference scale:
- 0.0: same content, different wording
- 0.25: same main ideas, secondary details differ
- 0.5: shared topic, but conclusions or approach differ
- 0.75: only the general topic is shared
- 1.0: unrelated or totally contradictory

Rules: base everything on the text, do not invent content, cite short
fragments as evidence, and flag uncertainty where it exists.

Respond ONLY with JSON in this form:
{
  "alignment": [{"a": "...", "b": "...", "relation": "..."}],
  "dimensions": {"topic": 0, "factual": 0, "conclusions": 0, "detail": 0, "tone": 0},
  "overall_distance": 0.0,
  "justification": "...",
  "confidence": "high|medium|low"
}