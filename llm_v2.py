# Uses config_v2 only

"""
Template-Based LLM with Ollama Primary — ANCHOR Honeypot
=========================================================
DESIGN:
1. Ollama LLM generates contextual persona replies (PRIMARY)
2. Template system provides deterministic fallback (SECONDARY)
3. LLM fills small template blanks when needed
4. State machine controls behavior, not LLM

FLOW:
    get_response(state, context) →
        try Ollama (20s timeout) →
        if valid → sanitize & return →
        else → fill template → sanitize & return

SECURITY:
- Jailbreak guard runs BEFORE this module is called
- Output is always sanitised (blocked patterns, length cap)
- Template fallback guarantees a response even if LLM is down
"""

import re
import random
import threading
import time
import logging
import difflib
from typing import Optional, Generator, Callable, Dict, List

# Use v2 config
import config_v2 as config

from state_machine_v2 import AgentState
from llm_service import OllamaClient, RED_FLAG_CONCEPTS, INVESTIGATIVE_TARGETS, PERSONA_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Red-flag keywords for post-generation validation (PART 2)
# ──────────────────────────────────────────────────────────────────
RED_FLAG_KEYWORDS = [
    "otp",
    "account compromise",
    "suspicious",
    "fraud",
    "unauthorized",
    "verification code",
    "security risk",
]

# Compiled regex for red-flag signal detection (PART 3)
# Use re.search(RED_FLAG_PATTERN, text) for single-signal detection
RED_FLAG_PATTERN = re.compile(
    r"(suspicious|compromis(?:ed|e)|fraud|unauthorized|verification\s?code|link|urgent|security|otp)",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────
# Output sanitizer — strips leaked system artifacts from LLM output
# ──────────────────────────────────────────────────────────────────

def sanitize_output(text: str) -> str:
    """
    Strip leaked system artifacts from LLM output.

    Small models (phi, tinyllama) frequently echo prompt fragments:
      - Bracketed stage directions: [Turn 2 — MIDDLE CALL]
      - Section headers: RULES:, RED FLAG AWARENESS:
      - Markdown formatting: **bold**, # headers
      - Role prefixes: You:, Assistant:

    This function removes all of them before validation runs.
    """
    # Remove inline bracketed content FIRST: [anything]
    # Must run before line-level filtering so partial lines survive.
    text = re.sub(r'\[.*?\]', '', text)

    # Remove lines that are empty after bracket stripping
    lines = text.split("\n")
    lines = [ln for ln in lines if ln.strip()]
    text = "\n".join(lines)

    # Remove lines that echo system prompt sections
    _echo_prefixes = (
        "rules:", "response rules:", "red flag", "investigative",
        "engagement", "behaviour:", "behavior:", "system:", "note:",
        "instruction", "reminder", "never:", "always:",
    )
    lines = text.split("\n")
    lines = [ln for ln in lines if not ln.strip().lower().startswith(_echo_prefixes)]
    text = "\n".join(lines)

    # Strip markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)            # *italic*
    text = re.sub(r'#+\s*', '', text)                    # # headers
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)  # code blocks
    text = re.sub(r'`(.+?)`', r'\1', text)               # `inline code`
    text = re.sub(r'^[-*]\s+', '', text, flags=re.MULTILINE)  # bullet points

    # Remove echoed role prefix
    text = re.sub(
        r'^(You|Me|Agent|Assistant|Elderly|Person):\s*',
        '', text.strip(), flags=re.IGNORECASE,
    )

    # Strip persona-breaking phrases (first line of defense)
    _ai_reveal_patterns = [
        r'(?i)\bAs an AI language model\b[,.]?\s*',
        r'(?i)\bI am (?:just )?an? AI\b[,.]?\s*',
        r'(?i)\bAs an? (?:AI|artificial intelligence)\b[,.]?\s*',
        r'(?i)\bI(?:\'m| am) (?:a |an? )?(?:virtual |digital )?assistant\b[,.]?\s*',
        r'(?i)\bI(?:\'m| am) programmed\b[,.]?\s*',
        r'(?i)\bprogrammed to\b',
        r'(?i)\bI cannot provide financial advice\b[,.]?\s*',
        r'(?i)\bI\'m not able to provide (?:financial |legal )?advice\b[,.]?\s*',
        r'(?i)\bdesigned to\b',
        r'(?i)\balgorithm\b',
        r'(?i)\blanguage model\b',
        r'(?i)\bchatbot\b',
    ]
    for pat in _ai_reveal_patterns:
        text = re.sub(pat, '', text)

    # Strip duplicate leading phrases
    # e.g. "Wait, you need a code from me? Wait, you need a code from me?"
    sentences = re.split(r'(?<=[.?!])\s+', text.strip())
    if len(sentences) >= 2 and sentences[0].strip().lower() == sentences[1].strip().lower():
        text = ' '.join(sentences[1:])

    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text


# Investigative phrases for validation
_INV_PHRASES = ["employee id", "branch", "manager", "callback number", "case id"]
_PERSONA_BREAKS = [
    "ai language model", "i cannot", "i am just an ai",
    "i'm an ai", "i am an ai", "as an ai", "chatbot",
    "programmed to", "designed to", "algorithm",
    "virtual assistant", "digital assistant",
    "i cannot provide financial advice",
    "language model",
]


def validate_response(response: str) -> bool:
    """
    STRICT post-generation quality gate.  Returns False if ANY check fails.

    Checks:
      1. Contains >= 1 red-flag keyword   (case-insensitive, from RED_FLAG_KEYWORDS)
      2. Contains >= 1 investigative phrase (employee id | branch | manager | callback number | case id)
      3. Contains a question mark
      4. Does NOT contain persona-breaking text
    """
    reply_lower = response.lower()

    # Check 1: Red-flag keyword (case-insensitive)
    if not any(keyword in reply_lower for keyword in RED_FLAG_KEYWORDS):
        return False

    # Check 2: Investigative phrase
    if not any(p in reply_lower for p in _INV_PHRASES):
        return False

    # Check 3: Question mark present
    if "?" not in response:
        return False

    # Check 4: No persona-breaking text
    if any(p in reply_lower for p in _PERSONA_BREAKS):
        return False

    return True


def _append_followup_question(response: str, turn_count: int = 0) -> str:
    """
    Append an investigative question guaranteeing an _INV_PHRASES keyword
    and a trailing '?'.  Skipped only if response already contains an
    investigative phrase AND ends with '?'.
    """
    lower = response.lower()
    has_inv = any(p in lower for p in _INV_PHRASES)
    has_q = response.strip().endswith("?")

    if has_inv and has_q:
        return response  # already valid

    # Every question contains an _INV_PHRASES keyword
    _followup_questions = [
        "What is your employee ID?",
        "Which branch are you calling from?",
        "What is your manager's name?",
        "Can you give me your official callback number?",
        "Do you have a case ID for this?",
        "What branch did you say you were at?",
        "Who is your manager there?",
    ]
    idx = turn_count % len(_followup_questions)
    if response and not response.endswith(" "):
        response += " "
    return response + _followup_questions[idx]


def _inject_red_flag_concern(response: str, turn_count: int = 0, scammer_message: str = "") -> str:
    """
    Prepend a red-flag phrase if response lacks a RED_FLAG_KEYWORDS hit.

    Rules (PART 1):
    - ALWAYS prepend if no red-flag keyword is present.
    - Phrase must contain >= 1 of: account compromise, suspicious, fraud,
      unauthorized, OTP, verification code, security risk.
    - Uses turn_count for deterministic rotation.
    - Handles persona breaks with full replacement.
    - Idempotent — will not double-inject if keyword already exists.
    - Does NOT depend on LLM to include keywords.
    """
    reply_lower = response.lower()

    # ── Persona break → full replacement (most critical) ────────────
    if any(p in reply_lower for p in _PERSONA_BREAKS):
        return "I'm confused. You mentioned an OTP and that worries me."

    # ── Already has a red-flag keyword → skip injection ─────────────
    if any(keyword in reply_lower for keyword in RED_FLAG_KEYWORDS):
        return response

    # ── Prepend a red-flag phrase (each contains a RED_FLAG_KEYWORDS word)
    _rf_inject_phrases = [
        "This sounds like an account compromise attempt.",
        "That seems very suspicious to me.",
        "I think this might be fraud.",
        "This feels like an unauthorized request.",
        "You mentioned an OTP and that worries me.",
        "Are you asking for a verification code from me?",
        "This sounds like a security risk.",
    ]
    idx = turn_count % len(_rf_inject_phrases)
    return _rf_inject_phrases[idx] + " " + response


class TemplateBasedLLM:
    """
    Hybrid LLM: Ollama primary, template fallback.

    get_response() — NEW top-level entry point used by anchor_agent.
    generate_response() — Legacy template-fill entry point (still works).
    """

    def __init__(
        self,
        model_path: str = config.LLM_MODEL_PATH,
        max_tokens: int = config.LLM_MAX_TOKENS,
        temperature: float = config.LLM_TEMPERATURE,
    ):
        self.model_path = model_path
        self.max_tokens = max_tokens
        self.temperature = temperature

        self.model = None
        self.backend = "template-only"
        self._lock = threading.Lock()

        # Streaming callback (kept for legacy compatibility)
        self.on_token: Optional[Callable] = None

        # Blocked patterns (compiled once)
        self._blocked_patterns = [
            re.compile(p, re.IGNORECASE) for p in config.BLOCKED_PATTERNS
        ]

        # Ollama client (primary engine)
        self._ollama = OllamaClient()

        # Anti-repetition: stores last 2 responses per session
        self._recent_responses: Dict[str, List[str]] = {}

        self._load_model()

    # ──────────────────────────────────────────────────────────────────
    # Model loading (llama-cpp → Ollama probe → template-only)
    # ──────────────────────────────────────────────────────────────────

    def _load_model(self):
        """Load LLM backend (optional — templates always work)."""
        # Try llama-cpp-python
        try:
            from llama_cpp import Llama

            self.model = Llama(
                model_path=self.model_path,
                n_ctx=config.LLM_CONTEXT_LENGTH,
                n_threads=2,
                n_batch=32,
                verbose=False,
            )
            self.backend = "llama-cpp"
            self.model("Hello", max_tokens=1)
            return
        except Exception:
            pass

        # Ollama availability already checked by OllamaClient lazily
        if self._ollama.is_available():
            self.backend = "ollama"
            return

        self.backend = "template-only"

    # ══════════════════════════════════════════════════════════════════
    # NEW PRIMARY ENTRY POINT — used by anchor_agent.py
    # ══════════════════════════════════════════════════════════════════

    def get_response(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        conversation_history: Optional[List[Dict[str, str]]] = None,
        latest_scammer_message: str = "",
        session_id: str = "default",
    ) -> str:
        """
        Generate a persona reply with deterministic red-flag enforcement.

        Pipeline (PART 4):
            1. Try Ollama → fallback to template.
            2. sanitize_output() → _sanitize() (strip artifacts + blocked patterns).
            3. Anti-repetition: if ≥70% similar to last 2 replies, regenerate once.
            4. If validate_response() fails:
               a. _inject_red_flag_concern() — prepend red-flag phrase.
               b. _append_followup_question() — append investigative question.
            5. assert validate_response() — zero-tolerance gate.
        """
        turn_count = len(conversation_history) // 2 if conversation_history else 0

        def _generate_raw(attempt: int = 0) -> str:
            """Generate raw response from Ollama or template."""
            result = ""
            # ── Attempt 1: Ollama LLM ───────────────────────────────
            try:
                llm_reply = self._ollama.call_ollama(
                    state=state.name,
                    conversation_history=conversation_history,
                    latest_scammer_message=latest_scammer_message,
                )
                if llm_reply and len(llm_reply.strip()) >= 5:
                    result = llm_reply
            except Exception as exc:
                logger.debug("Ollama primary failed: %s", exc)

            # ── Attempt 2: Template fallback ────────────────────────
            if not result:
                result = self.generate_response(state, template, fills, context="")
            return result

        def _is_too_similar(candidate: str) -> bool:
            """Check if candidate is ≥70% similar to any of last 2 responses."""
            recent = self._recent_responses.get(session_id, [])
            for prev in recent[-2:]:
                ratio = difflib.SequenceMatcher(None, prev.lower(), candidate.lower()).ratio()
                if ratio >= 0.70:
                    return True
            return False

        # ── Generate and sanitize ───────────────────────────────────
        raw = _generate_raw()
        clean = sanitize_output(raw)
        clean = self._sanitize(clean)

        # ── Anti-repetition: regenerate once if too similar ─────────
        if _is_too_similar(clean):
            raw2 = self.generate_response(state, template, fills, context="")
            alt = sanitize_output(raw2)
            alt = self._sanitize(alt)
            if not _is_too_similar(alt):
                clean = alt

        # ── Deterministic enforcement (PART 4 pipeline) ─────────────
        if not validate_response(clean):
            clean = _inject_red_flag_concern(clean, turn_count, latest_scammer_message)
            clean = _append_followup_question(clean, turn_count)

        assert validate_response(clean), f"Post-injection validation failed: {clean}"

        # ── Record response for anti-repetition ─────────────────────
        if session_id not in self._recent_responses:
            self._recent_responses[session_id] = []
        self._recent_responses[session_id].append(clean)
        # Keep only last 4 to bound memory
        if len(self._recent_responses[session_id]) > 4:
            self._recent_responses[session_id] = self._recent_responses[session_id][-4:]

        return clean

    # ══════════════════════════════════════════════════════════════════
    # LEGACY / TEMPLATE ENTRY POINT
    # ══════════════════════════════════════════════════════════════════

    def generate_response(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        context: str = "",
    ) -> str:
        """
        Generate response by filling template.

        FAST PATH (~1ms): Direct fill.
        LLM PATH (~100ms): Use local LLM to fill remaining blanks.
        """
        has_blanks = "{" in template and "}" in template

        if not has_blanks or self.backend == "template-only":
            response = self._fill_template(template, fills)
        else:
            response = self._generate_with_llm(state, template, fills, context)

        response = self._sanitize(response)
        return response

    # ──────────────────────────────────────────────────────────────────
    # Template helpers
    # ──────────────────────────────────────────────────────────────────

    def _fill_template(self, template: str, fills: Dict[str, str]) -> str:
        """Fill template placeholders, including fallback for missing keys."""
        result = template
        for key, value in fills.items():
            result = result.replace(f"{{{key}}}", value)

        # Safety net: replace any remaining {placeholder} with a random fill
        remaining = re.findall(r'\{(\w+)\}', result)
        for key in remaining:
            options = config.TEMPLATE_FILLS.get(key, ["something"])
            result = result.replace(f"{{{key}}}", random.choice(options), 1)

        return result

    def _generate_with_llm(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        context: str,
    ) -> str:
        """Fill template; use local LLM for remaining blanks."""
        partial = self._fill_template(template, fills)

        if "{" in partial:
            match = re.search(r'\{(\w+)\}', partial)
            if match:
                blank = match.group(1)
                filled = self._llm_fill_blank(blank, state)
                partial = partial.replace(f"{{{blank}}}", filled)

        return partial

    def _llm_fill_blank(self, blank_name: str, state: AgentState) -> str:
        """Ask local LLM to fill one blank (1-3 words). Never sees user text."""
        prompt = (
            f"Fill blank for confused elderly person.\n"
            f"State: {state.name}\nBlank: {blank_name}\n"
            f"Reply with ONLY the word/phrase (1-3 words max):"
        )

        with self._lock:
            if self.backend == "llama-cpp":
                result = self._generate_llama(prompt, 10)
            elif self.backend == "ollama":
                result = self._generate_ollama_legacy(prompt, 10)
            else:
                options = config.TEMPLATE_FILLS.get(blank_name, ["something"])
                idx = hash((state.name, blank_name)) % len(options)
                result = options[idx]

            result = result.split('\n')[0].strip()[:30]
            return result if result else "something"

    # ──────────────────────────────────────────────────────────────────
    # Backend helpers (llama-cpp / legacy ollama)
    # ──────────────────────────────────────────────────────────────────

    def _generate_llama(self, prompt: str, max_tokens: int) -> str:
        """Non-streaming llama-cpp."""
        try:
            output = self.model(
                prompt,
                max_tokens=max_tokens,
                temperature=self.temperature,
                stop=["\n", "###"],
                echo=False,
            )
            return output["choices"][0]["text"].strip()
        except Exception:
            return ""

    def _generate_ollama_legacy(self, prompt: str, max_tokens: int) -> str:
        """Non-streaming Ollama — used only for blank-filling."""
        try:
            import requests
            resp = requests.post(
                f"{self._ollama.base_url}/api/generate",
                json={
                    "model": self._ollama.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": self.temperature,
                    },
                },
                timeout=5,
            )
            return resp.json()["response"].strip() if resp.ok else ""
        except Exception:
            return ""

    # ──────────────────────────────────────────────────────────────────
    # Streaming (preserved for voice pipeline compatibility)
    # ──────────────────────────────────────────────────────────────────

    def generate_streaming(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        context: str = "",
    ) -> Generator[str, None, None]:
        """Generate with streaming tokens (for TTS pipeline)."""
        has_blanks = "{" in template and "}" in template

        if not has_blanks or self.backend == "template-only":
            response = self._fill_template(template, fills)
            yield self._sanitize(response)
            return

        if self.backend == "llama-cpp":
            yield from self._stream_llama(state, template, fills, context)
        elif self.backend == "ollama":
            yield from self._stream_ollama(state, template, fills, context)
        else:
            yield self._sanitize(self._fill_template(template, fills))

    def _stream_llama(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        context: str,
    ) -> Generator[str, None, None]:
        """Stream from llama-cpp."""
        prompt = self._build_prompt(state, context)
        accumulated = ""

        try:
            for output in self.model(
                prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stop=["\n", "###", "Caller:"],
                stream=True,
            ):
                token = output["choices"][0]["text"]
                accumulated += token
                yield self._sanitize(accumulated)
        except Exception:
            yield self._sanitize(self._fill_template(template, fills))

    def _stream_ollama(
        self,
        state: AgentState,
        template: str,
        fills: Dict[str, str],
        context: str,
    ) -> Generator[str, None, None]:
        """Stream from Ollama."""
        import requests
        import json as _json

        prompt = self._build_prompt(state, context)
        accumulated = ""

        try:
            resp = requests.post(
                f"{self._ollama.base_url}/api/generate",
                json={
                    "model": self._ollama.model,
                    "prompt": prompt,
                    "stream": True,
                    "options": {
                        "num_predict": self.max_tokens,
                        "temperature": self.temperature,
                    },
                },
                stream=True,
                timeout=20,
            )

            for line in resp.iter_lines():
                if line:
                    data = _json.loads(line)
                    token = data.get("response", "")
                    accumulated += token
                    yield self._sanitize(accumulated)
                    if data.get("done"):
                        break
        except Exception:
            yield self._sanitize(self._fill_template(template, fills))

    def _build_prompt(self, state: AgentState, context: str) -> str:
        """Minimal prompt for streaming path.

        Uses the same plain-language system prompt as the primary path.
        No meta-labels, no brackets — prevents leakage on small models.
        """
        recent = context[-200:].strip() if context else ""
        if recent:
            return f"{PERSONA_SYSTEM_PROMPT}\n\n{recent}\n\nYou:"
        return f"{PERSONA_SYSTEM_PROMPT}\n\nYou:"

    # ──────────────────────────────────────────────────────────────────
    # Output sanitization
    # ──────────────────────────────────────────────────────────────────

    def _sanitize(self, text: str) -> str:
        """Remove blocked patterns, cap length."""
        for pattern in self._blocked_patterns:
            text = pattern.sub("", text)
        text = re.sub(r'\b\d{4,}\b', '', text)
        text = text.strip()
        text = re.sub(r'\s+', ' ', text)
        if len(text) > 150:
            text = text[:150].rsplit(' ', 1)[0] + "..."
        return text


def create_llm() -> TemplateBasedLLM:
    """Factory function."""
    return TemplateBasedLLM()
