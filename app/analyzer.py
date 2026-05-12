"""
AI analysis engine.
Finds viral clip moments and writes captions.
Uses app.ai_brain — switch the model in .env without touching this file.
"""
import logging
from app.models import ClipSegment, TranscriptSegment
from app import ai_brain

logger = logging.getLogger(__name__)


def _get_lessons() -> str:
    try:
        from app.hook_learner import build_hook_lessons
        return build_hook_lessons()
    except Exception:
        return ""


# ── Brand context ──────────────────────────────────────────────────────────────

BRAND_CONTEXT = """
You are helping the speaker, a content creator whose niche is:
- Startups & entrepreneurship
- Talent visas (Global Talent Visa, O-1, etc.)
- Being globally attractive as a professional/founder
- Tech, innovation, and building a personal brand
- Opportunities for Africans in the global tech ecosystem

Their audience: ambitious professionals, founders, and immigrants who want to build globally.
Their tone: direct, confident, motivational, no-fluff. They speak like founders who've been through it.
"""

HOOK_TRAINING = """
HOOK PRINCIPLES (first 0.5 seconds = make or break):
- Pattern interrupts: Say something unexpected or counterintuitive
- Bold claims: "Most people are wrong about X"
- Curiosity gaps: "The one thing nobody tells you about talent visas..."
- Direct address: "If you're a founder trying to move to the UK..."
- Stakes: Make the viewer feel like they'll lose something if they don't watch
- Specificity beats vague: "£50K in 6 months" beats "make money online"

WEAK hooks (avoid): "In this video I'll talk about..." / "Hey guys today we..." / "So I wanted to share..."
STRONG hooks: Start mid-thought, with tension, with a number, with a contradiction
"""


# ── Clip finder ────────────────────────────────────────────────────────────────

def find_viral_clips(
    full_text: str,
    segments: list[TranscriptSegment],
    video_duration: float,
) -> list[ClipSegment]:
    """Find the best viral clip moments in a transcript."""
    timestamped = "\n".join(
        f"[{seg.start:.1f}s - {seg.end:.1f}s]: {seg.text}"
        for seg in segments
    )

    lessons = _get_lessons()
    lessons_block = f"\n{lessons}\n" if lessons else ""

    provider_info = ai_brain.get_provider_info()
    logger.info(f"[Analyzer] Finding viral clips via {provider_info['provider']} ({provider_info.get('model','')})")

    prompt = f"""
{BRAND_CONTEXT}

{HOOK_TRAINING}
{lessons_block}
Below is a timestamped transcript from one of MrBade's videos (total duration: {video_duration:.0f} seconds).

TIMESTAMPED TRANSCRIPT:
{timestamped}

YOUR TASK:
Identify 3-6 viral clip opportunities. For each clip:
1. Find a segment where the hook (first sentence spoken) is STRONG
2. STRICT DURATION RULE: end_time - start_time MUST be >= 40 seconds. No exceptions.
   Before finalising each clip, verify: (end_time - start_time) >= 40.
   If any clip is shorter than 40 seconds, extend end_time until it is at least 40 seconds.
   The viewer needs enough context to understand the point being made.
   A clip that cuts off before the key insight lands is worthless.
   IDEAL: 45-75 seconds. ABSOLUTE MINIMUM: 40 seconds.
3. The clip must deliver ONE complete, valuable insight — not just the start of one.
   Include enough speech that the point is fully made and lands clearly.
4. Extend end_time generously — better to run a few seconds long than cut too early.
5. Clips can overlap in time but should be different angles/hooks

Return ONLY a valid JSON array. No explanation, no markdown, just the JSON:
[
  {{
    "start_time": 12.5,
    "end_time": 45.0,
    "hook_text": "The exact first sentence spoken in this clip",
    "hook_score": 8,
    "hook_reason": "Why this hook will stop the scroll",
    "topic": "One-line topic",
    "value_proposition": "What the viewer gains from watching this clip"
  }}
]
"""

    clips_data = ai_brain.complete_json(prompt, max_tokens=2000)
    if not isinstance(clips_data, list):
        raise ValueError(f"Expected a JSON array from AI, got: {type(clips_data)}")

    clips = [ClipSegment(**c) for c in clips_data]
    logger.info(f"[Analyzer] Found {len(clips)} viral clip candidates.")
    return clips


# ── Caption writer ─────────────────────────────────────────────────────────────

def write_caption(
    clip_segment: ClipSegment,
    transcript_excerpt: str,
) -> tuple[str, list[str], str]:
    """
    Write a natural, human-sounding caption for a clip.
    Returns: (caption, hashtags, full_post_text)
    """
    prompt = f"""
{BRAND_CONTEXT}

You need to write a TikTok post caption for the following clip.

CLIP DETAILS:
- Topic: {clip_segment.topic}
- Hook (first line of the clip): {clip_segment.hook_text}
- Why it's valuable: {clip_segment.value_proposition}
- Clip transcript excerpt: {transcript_excerpt}

CAPTION RULES:
1. Write like the speaker themselves wrote it — direct, no fluff, founder energy
2. DO NOT sound like AI. No "In today's video", no "I hope this helps", no bullet points with emojis as headers
3. First line = the hook (rewrite the hook_text slightly for text format)
4. 2-4 short punchy lines max. Leave space. TikTok is not a blog.
5. End with 1 call to action (follow, comment, share your experience — pick ONE)
6. Then on a new line, 5-8 relevant hashtags

Return ONLY valid JSON:
{{
  "caption": "The caption text only (no hashtags)",
  "hashtags": ["startup", "globaltalent", "talentvisa", "founder", "africaintech"]
}}
"""

    logger.info(f"[Analyzer] Writing caption via {ai_brain.get_provider_info()['provider']}")
    data = ai_brain.complete_json(prompt, max_tokens=600)

    caption  = data["caption"]
    hashtags = data["hashtags"]
    full_post_text = caption + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in hashtags)

    return caption, hashtags, full_post_text
