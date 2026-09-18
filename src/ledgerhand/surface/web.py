"""Web driver: Playwright + an in-page accessibility projection.

Two deliberate choices worth defending:

1. Perception is role/name-first, not selector-first. The Observation the agent
   and the replay engine see contains no CSS. Selectors are recorded as a
   low-ranked fallback inside locators, never as the primary plan, because on
   the apps this targets they are the least durable thing on the page.

2. Acting is real input at real coordinates wherever the surface has a visual
   affordance -- Playwright's mouse and keyboard, not element.click(). That is
   what "computer use" means, and it is the part that ports to a desktop
   driver. The one exception is a native <select>, which has no screen-level
   affordance a mouse can drive reliably; there the driver falls back to the
   platform widget API. A desktop driver makes the same trade with AX SetValue.
"""
from __future__ import annotations

import pathlib
import time
from typing import Any

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, TimeoutError as PWTimeout, sync_playwright

from ..models.enums import ActionKind, SurfaceKind
from ..models.observation import Observation, UINode
from .base import ActRequest, ActResult, SurfaceCaps

_JS = (pathlib.Path(__file__).parent / "perception.js").read_text()


class WebSurface:
    """A Chromium-backed Surface."""

    kind = SurfaceKind.WEB

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 900),
        slow_mo_ms: int = 0,
    ) -> None:
        self._headless = headless
        self._viewport = viewport
        self._slow_mo = slow_mo_ms
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Page | None = None
        self._last_status: int | None = None
        self._last_status_url: str = ""
        #: URL observed when the last action fired; settle() uses it to spot a commit.
        self._url_at_last_act: str = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless, slow_mo=self._slow_mo
        )
        self._ctx = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            ignore_https_errors=False,
        )
        self._page = self._ctx.new_page()
        self._page.on("response", self._on_response)

    def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None

    def caps(self) -> SurfaceCaps:
        return SurfaceCaps(extras={"engine": "chromium", "perception": "in-page-ax-projection"})

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("surface not started")
        return self._page

    def _on_response(self, response: Any) -> None:
        """Track the status of main-document responses.

        This is how an HTTP 500 becomes a *detected* hard failure instead of a
        page whose text happens not to match a checkpoint.
        """
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self._last_status = response.status
                self._last_status_url = response.url
        except Exception:
            pass

    # -- navigation / settling ---------------------------------------------

    def navigate(self, url: str) -> ActResult:
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            status = resp.status if resp else None
            if status and status >= 500:
                return ActResult(True, f"navigated to {url}",
                                 transport_error=f"HTTP {status}")
            return ActResult(True, f"navigated to {url}")
        except PWTimeout:
            return ActResult(False, error=f"navigation to {url} timed out")
        except PWError as exc:
            return ActResult(False, error=f"navigation failed: {exc}")

    def settle(self, timeout_ms: int = 12_000) -> None:
        """Wait for the surface to go quiet.

        The subtle part is the *commit window*. A click that submits a form has
        not navigated yet when act() returns, so waiting on load state alone
        returns instantly against the old document and the next perceive() sees
        a stale page. So we first wait, briefly and boundedly, for a navigation
        to actually commit -- then wait for it to finish.

        This is best-effort by design. The artifact's explicit `until`
        conditions are the real determinism mechanism; this just stops us
        observing a page mid-transition.
        """
        before = self._url_at_last_act
        deadline = time.monotonic() + min(2.0, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            try:
                if self.page.url != before:
                    break
                state = self.page.evaluate("() => document.readyState")
                if state == "loading":
                    break
            except PWError:
                break
            time.sleep(0.05)
        for state_name in ("domcontentloaded", "networkidle"):
            try:
                self.page.wait_for_load_state(state_name, timeout=timeout_ms)
            except (PWTimeout, PWError):
                pass
        self._await_frames(deadline=time.monotonic() + min(3.0, timeout_ms / 1000.0))

    def _await_frames(self, *, deadline: float) -> None:
        """Wait for child frames to have a usable document.

        Page-level load states describe the top document. In a framed app the
        content the automation actually cares about lives in a child frame that
        can still be parsing when the page reports itself idle -- and a frame
        that is not ready simply contributes no controls to the observation.
        The agent then sees a screen with nothing on it that it recognises,
        which looks exactly like being stuck. It is worth three seconds to not
        confuse "not loaded yet" with "dead end".
        """
        while time.monotonic() < deadline:
            frames = self._frames()
            pending = False
            expected = 0
            for frame in frames:
                try:
                    state = frame.evaluate("() => document.readyState")
                    expected += frame.evaluate(
                        "() => document.querySelectorAll('iframe,frame').length")
                    if state == "loading":
                        pending = True
                except (PWError, PWTimeout):
                    pending = True     # not navigable yet
            # A frame element can be in the DOM before its Frame is registered.
            # Counting the elements is the only way to know we are still short
            # of frames -- and a missing frame is invisible rather than noisy:
            # it simply contributes no controls, so the agent sees a screen it
            # does not recognise and looks stuck.
            if len(frames) - 1 < expected:
                pending = True
            if not pending:
                return
            time.sleep(0.05)

    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    # -- perception ---------------------------------------------------------

    def _frames(self) -> list[Frame]:
        try:
            return [f for f in self.page.frames if not f.is_detached()]
        except Exception:
            return [self.page.main_frame]

    def _frame_offset(self, frame: Frame) -> tuple[int, int]:
        """Absolute offset of a frame's viewport inside the top-level page."""
        if frame == self.page.main_frame:
            return (0, 0)
        try:
            el = frame.frame_element()
            box = el.bounding_box()
            return (int(box["x"]), int(box["y"])) if box else (0, 0)
        except Exception:
            return (0, 0)

    @staticmethod
    def _frame_label(frame: Frame, index: int) -> str:
        return frame.name or f"frame{index}"

    def perceive(self, step: int = 0) -> Observation:
        controls: list[UINode] = []
        chrome_text: list[str] = []
        content_text: list[str] = []
        frame_labels: list[str] = []
        seen_lines: set[str] = set()
        content_url = ""

        for i, frame in enumerate(self._frames()):
            label = self._frame_label(frame, i)
            is_main = frame == self.page.main_frame
            path: list[str] = [] if is_main else [label]
            data = None
            for attempt in range(2):
                try:
                    data = frame.evaluate(_JS, {"prefix": f"f{i}_"})
                    break
                except (PWError, PWTimeout):
                    # Frames navigate underneath us; one retry covers the gap.
                    if attempt == 0:
                        time.sleep(0.15)
            if data is None:
                continue
            if not isinstance(data, dict):
                continue
            if not is_main:
                frame_labels.append(label)
                # Deepest frame wins: that is where the work happens.
                content_url = str(data.get("url") or "")
            ox, oy = self._frame_offset(frame)

            for raw in list(data.get("controls") or []) + list(data.get("readables") or []):
                node = self._to_node(raw, path, ox, oy)
                if node:
                    controls.append(node)
            bucket = chrome_text if is_main else content_text
            for line in data.get("lines") or []:
                key = line.strip()
                if key and key not in seen_lines and len(key) > 1:
                    seen_lines.add(key)
                    bucket.append(key)

        transport = None
        if self._last_status and self._last_status >= 400:
            transport = f"HTTP {self._last_status} on {self._last_status_url}"

        return Observation(
            step=step,
            url=self.current_url(),
            title=self._title(),
            controls=controls,
            # Content before chrome: a truncated text budget must not spend
            # itself on the navigation menu and hide the screen's own heading.
            texts=content_text + chrome_text,
            frames=frame_labels,
            content_url=content_url,
            transport_error=transport,
        )

    def _title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    @staticmethod
    def _to_node(raw: dict, frame_path: list[str], ox: int, oy: int) -> UINode | None:
        try:
            box = raw.get("box") or [0, 0, 0, 0]
            attrs = {k: str(v) for k, v in (raw.get("attrs") or {}).items() if v not in (None, "")}
            return UINode(
                handle=raw["handle"],
                role=raw.get("role") or "generic",
                name=raw.get("name") or "",
                inferred_label=raw.get("inferred_label") or "",
                value=raw.get("value") or "",
                text=raw.get("text") or "",
                enabled=bool(raw.get("enabled", True)),
                focused=bool(raw.get("focused", False)),
                editable=bool(raw.get("editable", False)),
                box=(int(box[0]) + ox, int(box[1]) + oy, int(box[2]), int(box[3])),
                frame_path=list(frame_path),
                attrs=attrs,
            )
        except Exception:
            return None

    def page_text(self) -> str:
        """Concatenated readable text across every frame."""
        chunks: list[str] = []
        for frame in self._frames():
            try:
                chunks.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except (PWError, PWTimeout):
                continue
        return "\n".join(c for c in chunks if c)

    # -- acting -------------------------------------------------------------

    def _locate(self, node: UINode) -> Any:
        """Re-bind a perceived node to its live element via the perception stamp.

        The stamp is written during perceive() and is only valid for the current
        observation -- which is exactly the guarantee we want: you cannot act on
        a node you have not just looked at.
        """
        for i, frame in enumerate(self._frames()):
            label = self._frame_label(frame, i)
            in_frame = (not node.frame_path) if frame == self.page.main_frame else (
                node.frame_path and node.frame_path[0] == label
            )
            if not in_frame:
                continue
            loc = frame.locator(f'[data-lh-h="{node.handle}"]')
            try:
                if loc.count() == 1:
                    return loc
            except PWError:
                continue
        return None

    def act(self, request: ActRequest) -> ActResult:
        kind = request.kind
        self._url_at_last_act = self.current_url()
        if kind is ActionKind.NAVIGATE:
            return self.navigate(request.url or "")

        if kind is ActionKind.PRESS:
            try:
                self.page.keyboard.press(request.key or "Enter")
                return ActResult(True, f"pressed {request.key}")
            except PWError as exc:
                return ActResult(False, error=str(exc))

        node = request.node
        if node is None:
            return ActResult(False, error=f"{kind.value} requires a target node")
        loc = self._locate(node)
        if loc is None:
            return ActResult(False, error=f"node {node.handle} is no longer present")

        try:
            loc.scroll_into_view_if_needed(timeout=4_000)
        except (PWError, PWTimeout):
            pass

        if kind is ActionKind.SELECT:
            return self._do_select(loc, request)
        if kind is ActionKind.CLICK:
            return self._do_click(loc, node)
        if kind is ActionKind.TYPE:
            return self._do_type(loc, node, request)
        return ActResult(False, error=f"unsupported action {kind.value}")

    def _do_click(self, loc: Any, node: UINode) -> ActResult:
        """Real mouse input at the control's real position."""
        try:
            box = loc.bounding_box(timeout=4_000)
            if not box:
                return ActResult(False, error=f"{node.handle} has no visible geometry")
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            self.page.mouse.click(x, y)
            return ActResult(True, f"clicked {node.label or node.handle} at ({x:.0f},{y:.0f})")
        except PWTimeout:
            return ActResult(False, error=f"timed out clicking {node.handle}")
        except PWError as exc:
            return ActResult(False, error=str(exc))

    def _do_type(self, loc: Any, node: UINode, request: ActRequest) -> ActResult:
        """Focus by clicking, clear, then send real keystrokes."""
        text = request.value or ""
        try:
            box = loc.bounding_box(timeout=4_000)
            if box:
                self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                loc.focus(timeout=4_000)
            self.page.keyboard.press("ControlOrMeta+a")
            self.page.keyboard.press("Delete")
            self.page.keyboard.type(text, delay=12)
            shown = "<redacted>" if request.sensitive else text
            return ActResult(True, f"typed {shown!r} into {node.label or node.handle}")
        except PWTimeout:
            return ActResult(False, error=f"timed out typing into {node.handle}")
        except PWError as exc:
            return ActResult(False, error=str(exc))

    def _do_select(self, loc: Any, request: ActRequest) -> ActResult:
        """Native combobox: no mouse-driveable affordance, so use the widget API."""
        value = request.value or ""
        for attempt in ("label", "value"):
            try:
                loc.select_option(**{attempt: value}, timeout=3_000)
                return ActResult(True, f"selected {value!r} by {attempt}")
            except (PWError, PWTimeout):
                continue
        return ActResult(False, error=f"no option matching {value!r}")

    # -- evidence -----------------------------------------------------------

    def screenshot(self, path: str) -> str | None:
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            return None

    def snapshot(self, path: str) -> str | None:
        """Markup dump across frames -- the richer signal kept on failure."""
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            parts = []
            for i, frame in enumerate(self._frames()):
                try:
                    parts.append(f"<!-- frame {i}: {frame.url} -->\n{frame.content()}")
                except PWError:
                    continue
            pathlib.Path(path).write_text("\n\n".join(parts), encoding="utf-8")
            return path
        except Exception:
            return None
