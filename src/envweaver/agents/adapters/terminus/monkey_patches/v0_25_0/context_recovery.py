"""Replace stale fallback history and propagate errors swallowed by Harbor."""

from functools import wraps


def install() -> None:
    """Wrap the official query boundary once, outside its internal retry decorator."""
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.llms.base import ContextLengthExceededError

    original = Terminus2._query_llm
    if getattr(original, "_envweaver_context_recovery", False):
        return

    @wraps(original)
    async def query(self, chat, *args, **kwargs):
        context_exceeded = False
        recovery_error = None
        full_summary = False
        original_chat = chat.chat
        previous = chat.__dict__.get("chat")
        original_summarize = self._summarize
        previous_summarize = self.__dict__.get("_summarize")

        @wraps(original_summarize)
        async def summarize(*summary_args, **summary_kwargs):
            nonlocal full_summary
            result = await original_summarize(*summary_args, **summary_kwargs)
            full_summary = True
            return result

        @wraps(original_chat)
        async def observe(*call_args, **call_kwargs):
            nonlocal context_exceeded, recovery_error
            if context_exceeded and not full_summary:
                # Harbor's short/terminal-only fallback produces a handoff prompt,
                # but does not replace the old chat. Keep the original instructions
                # and let chat.chat append the handoff plus the recovered response.
                chat._messages = chat.messages[:1]
                chat.reset_response_chain()
            try:
                result = await original_chat(*call_args, **call_kwargs)
            except Exception as error:
                # A second main-chat failure follows Harbor's recovery attempt.
                # Summary calls use self._llm.call directly and do not enter here.
                if context_exceeded:
                    recovery_error = error
                if isinstance(error, ContextLengthExceededError):
                    context_exceeded = True
                raise
            else:
                context_exceeded = False
                recovery_error = None
                return result

        chat.chat = observe
        self._summarize = summarize
        try:
            result = await original(self, chat, *args, **kwargs)
        finally:
            if previous is None:
                del chat.chat
            else:
                chat.chat = previous
            if previous_summarize is None:
                del self._summarize
            else:
                self._summarize = previous_summarize

        if recovery_error is not None:
            # Harbor catches the recovery failure and returns placeholder text.
            # Raise outside its retry boundary using the original error type.
            raise recovery_error
        return result

    query._envweaver_context_recovery = True
    Terminus2._query_llm = query
