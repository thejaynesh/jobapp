"""
Surviving an extension reload, in the tabs that were already open.

Reloading an extension does not remove the content scripts it already
injected. They stay in every open tab, still running, with the connection back
to the extension severed — `chrome.runtime.id` becomes `undefined` from that
moment, and any `sendMessage` throws "Extension context invalidated".

The reason this needed its own guard, rather than the error handling already
around those calls: **that throw is synchronous.** `chrome.runtime.lastError`
only ever reports a call that was delivered and found no receiver. Here the
call never leaves, so the reply callback is not run, and the error escapes past
every handler written around the reply — out of a Promise executor as a
rejection nobody catches, which is exactly what showed up in the console.

It matters most in the relay, which fires once per intercepted response: an
orphaned one throws continuously, for as long as the tab is open, on a site the
user is actively browsing.

Assertions against source text rather than behaviour, because none of this can
be executed here — it only runs inside Chrome, in a tab whose extension has
been reloaded out from under it.
"""

import re

import pytest


def source(name):
    return open(f"extension/{name}").read()


CALLERS = ["overlay.js", "relay.js"]


class TestEveryContentScriptChecksItIsStillConnected:
    @pytest.mark.parametrize("name", CALLERS)
    def test_it_can_tell_whether_the_extension_is_still_there(self, name):
        text = source(name)
        assert "chrome.runtime.id" in text, (
            f"{name} has no way to notice it was orphaned"
        )

    @pytest.mark.parametrize("name", CALLERS)
    def test_the_check_itself_cannot_throw(self, name):
        # Reading `chrome.runtime` can throw once the context is gone, so the
        # guard has to be inside its own try — a guard that throws is not a
        # guard.
        text = source(name)
        match = re.search(r"function connected\(\)\s*\{(.+?)\n\}", text, re.S)
        assert match, f"{name} has no connected() helper"
        assert "try" in match.group(1)

    @pytest.mark.parametrize("name", CALLERS)
    def test_every_send_is_wrapped(self, name):
        # The throw is synchronous, so a bare sendMessage is an uncaught error
        # however carefully its reply is handled.
        text = source(name)
        sends = text.split("chrome.runtime.sendMessage")[1:]
        assert sends, f"{name} does not call sendMessage at all"
        # Each call site must sit after a `try {` that has not yet closed —
        # approximated by requiring as many `try` as sendMessage calls.
        assert text.count("try") >= len(sends), (
            f"{name} has {len(sends)} sendMessage call(s) and fewer try blocks"
        )


class TestTheOverlaySaysWhatHappened:
    def test_it_explains_the_reload_rather_than_failing_silently(self):
        # A panel that goes blank looks like a broken extension. "Refresh this
        # page" is the whole fix and the user cannot guess it.
        text = source("overlay.js")
        assert "refresh this page" in text.lower()

    def test_the_promise_still_settles_when_orphaned(self):
        # `ask` is awaited. Throwing out of the executor leaves the caller
        # waiting forever, so the guard has to resolve rather than return.
        text = source("overlay.js")
        ask = text.split("async function ask(")[1].split("\n  }")[0]
        assert "resolve(" in ask
        assert "return { error:" in ask or "return {error:" in ask


class TestTheRelayStopsRatherThanRepeats:
    def test_it_removes_its_own_listener_once_orphaned(self):
        # It fires per intercepted response. Failing one at a time would throw
        # continuously for as long as the tab stays open.
        text = source("relay.js")
        assert "removeEventListener" in text

    def test_the_listener_is_named_so_it_can_be_removed(self):
        # An anonymous listener cannot be unregistered, which is why this was
        # written as a named function rather than inline.
        text = source("relay.js")
        assert "function onMessage(" in text
        assert 'addEventListener("message", onMessage)' in text


class TestTheExtensionPagesAreLeftAlone:
    def test_options_needs_no_guard(self):
        # options.js runs in an extension page. That page is torn down and
        # rebuilt by a reload rather than orphaned by it, so there is no dead
        # context to defend against and a guard would be cargo cult.
        assert "function connected()" not in source("options.js")
