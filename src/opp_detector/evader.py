'''
Evasion: get whatever is on screen out of sight and silence it, right now.

`choose_evasion_method()` picks the best method this machine allows and returns it; call the
returned function (no args) every time an opp shows up. It returns a `restore` function (no args,
never raises) that puts things back once they're gone: unmutes unless it was already muted, switches
back unless you've moved desktops yourself since, unhides the hidden app.

Methods, best first:
	_evade_pyautogui      mute + ctrl+right via pyautogui (switch desktop)      needs Accessibility
	_evade_quartz         mute + ctrl+right as one flagged Quartz event        needs Accessibility
	_evade_hide_app       mute + hide the frontmost app (like cmd+H)           no permissions
	_evade_sleep_display  mute + turn the display off                          no permissions

The desktop-switch methods go left instead when already on the last desktop, check that the
desktop really changed, and hide the front app if it didn't (one desktop only, shortcut ignored).

Checked live on macOS 27: both key methods switch desktops; plain pyautogui.hotkey is enough.

Things that were researched and deliberately left out:
	- pausing media: no way to do it without Accessibility that isn't a play/pause *toggle*
	  (MediaRemote is locked down since macOS 15.4), so we only mute
	- switching desktops without Accessibility: the private CGS/SkyLight calls no longer work
'''
import logging
import multiprocessing
import os
import subprocess
import sys
import time
from typing import Callable

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"

# modifiers of the "switch desktop" shortcut per platform; the arrow key gets appended
# (mac: Mission Control's "Move left/right a space")
_SWITCH_MODIFIERS = {
	"darwin": ("ctrl",),
	"win32": ("ctrl", "win"),
}.get(sys.platform, ("ctrl", "alt")) # linux: GNOME/KDE default-ish, not guaranteed

# How long to wait for the desktop to change before deciding the keypress was ignored.
# The desktop ID only flips once the slide animation ends (~0.55s measured), so keep this generous:
# too short = we think it failed while it's still sliding
_SWITCH_TIMEOUT = 1.5


def choose_evasion_method() -> Callable[[], Callable[[], None]]:
	'''Determines and returns the function to call for evasion'''
	if not IS_MAC:
		# windows/linux don't gate synthetic keypresses behind a permission
		return _evade_pyautogui

	if not _has_pyobjc():
		log.warning("pyobjc missing (it comes with pyautogui): can only sleep the display")
		return _evade_sleep_display

	if not can_post_keypresses():
		log.warning(
			"No Accessibility permission, so keypresses would be silently dropped: hiding the "
			"front app instead of switching desktops. Grant it to your TERMINAL app (not python) "
			"in System Settings > Privacy & Security > Accessibility, then restart the terminal."
		)
		return _evade_hide_app

	if not _next_desktop_shortcut_enabled():
		log.warning("'Move right a space' shortcut is disabled/rebound: hiding the front app instead")
		return _evade_hide_app

	return _evade_pyautogui if _has_pyautogui() else _evade_quartz


# ---------------------------------------------------------------- evasion methods

def _evade_pyautogui() -> Callable[[], None]:
	'''Mute, then switch desktops with pyautogui'''
	unmute = _mute()
	undo = _switch_desktop(_press_pyautogui)
	if not undo and IS_MAC: undo = _hide_front_app()
	return _restorer(unmute, undo)

def _evade_quartz() -> Callable[[], None]:
	'''Mute, then switch desktops with a hand-built Quartz key event (mac only)'''
	unmute = _mute()
	return _restorer(unmute, _switch_desktop(_press_quartz) or _hide_front_app())

def _evade_hide_app() -> Callable[[], None]:
	'''Mute, then hide the frontmost app. Needs no permissions (mac only)'''
	unmute = _mute()
	return _restorer(unmute, _hide_front_app())

def _evade_sleep_display() -> Callable[[], None]:
	'''Mute, then turn the display off. Last resort: needs nothing, but isn't subtle (mac only)'''
	unmute = _mute()
	subprocess.Popen(["pmset", "displaysleepnow"])
	return _restorer(unmute) # no undo for the display: whoever's there wakes it by touching anything

def _restorer(*undos:Callable[[], None]|None) -> Callable[[], None]:
	'''Bundles an evasion's undos (None = nothing to undo) into one restore that never raises'''
	def restore() -> None:
		for undo in reversed(undos): # last done, first undone: screen back before sound
			if undo is None: continue
			try:
				undo()
			except Exception as e:
				log.warning("restore failed: %s", e)
	return restore


# ---------------------------------------------------------------- what are we allowed to do?

def can_post_keypresses() -> bool:
	'''
	Whether synthetic keypresses (pyautogui / Quartz) will actually be delivered.

	Without the permission macOS drops them silently -- no exception -- so this is the only way
	to know. Never prompts. Caveats: the permission belongs to the app that launched python
	(Terminal / iTerm / VS Code), and the answer is fixed when the process starts.
	'''
	if not IS_MAC: return True
	try:
		import Quartz
		return bool(Quartz.CGPreflightPostEventAccess())
	except Exception:
		return False

def _next_desktop_shortcut_enabled() -> bool:
	'''Whether Mission Control's "Move right a space" is still on and still ctrl+right'''
	try:
		from Foundation import CFPreferencesCopyAppValue
		hotkeys = CFPreferencesCopyAppValue("AppleSymbolicHotKeys", "com.apple.symbolichotkeys")
		entry = (hotkeys or {}).get("81") # 81 = move right a space
		if entry is None: return True # never touched -> system default
		if not entry.get("enabled", True): return False
		params = (entry.get("value") or {}).get("parameters")
		if not params: return True
		keycode, modifiers = int(params[1]), int(params[2])
		# compare shift/ctrl/opt/cmd only: the fn + numpad bits vary
		return keycode == 0x7C and modifiers & 0x1E0000 == 0x40000
	except Exception:
		return True # can't tell; the post-switch check catches it anyway

def _has_pyautogui() -> bool:
	try:
		import pyautogui
		return True
	except Exception: # ImportError, or no display on linux
		return False

def _has_pyobjc() -> bool:
	try:
		import AppKit, Quartz
		return True
	except Exception:
		return False


# ---------------------------------------------------------------- building blocks

def _mute() -> Callable[[], None]|None:
	'''
	Mute system output; returns the undo. Needs no permissions; fired off without waiting so it costs
	nothing. The same osascript prints whether it was muted already, read by the undo (done by then)
	'''
	if not IS_MAC: return None # elsewhere the only generic option is the mute KEY, which is a toggle
	proc = subprocess.Popen([
		"osascript",
		"-e", "set was to output muted of (get volume settings)",
		"-e", "set volume output muted true",
		"-e", "was",
	], stdout=subprocess.PIPE, text=True)
	def unmute() -> None:
		# only if WE muted it ("missing value" = device can't mute: leave that alone too)
		if proc.communicate(timeout=2)[0].strip() == "false":
			subprocess.Popen(["osascript", "-e", "set volume output muted false"])
	return unmute

def _press_pyautogui(arrow:str) -> None:
	'''Presses the switch-desktop shortcut; `arrow` is "left" or "right"'''
	import pyautogui
	# _pause=False skips pyautogui's 0.1s post-call nap
	pyautogui.hotkey(*_SWITCH_MODIFIERS, arrow, _pause=False)

def _press_quartz(arrow:str) -> None:
	'''ctrl+arrow as a single event with explicit flags (real arrow keys carry fn + numpad too)'''
	import Quartz
	keycode = 0x7C if arrow == "right" else 0x7B
	flags = (Quartz.kCGEventFlagMaskControl
			| Quartz.kCGEventFlagMaskSecondaryFn
			| Quartz.kCGEventFlagMaskNumericPad)
	for is_down in (True, False):
		event = Quartz.CGEventCreateKeyboardEvent(None, keycode, is_down)
		Quartz.CGEventSetFlags(event, flags)
		Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
		time.sleep(0.01)

def _switch_desktop(press:Callable[[str], None], arrow:str|None=None) -> Callable[[], None]|None:
	'''
	Switches to a neighbouring desktop using `press` (towards `arrow`, or wherever there's room);
	returns the undo (switches back), or None if the desktop didn't change
	'''
	desktops = _desktops()
	if arrow is None:
		arrow = "right"
		if desktops is not None:
			current, all_ids = desktops
			if len(all_ids) < 2: return None # nowhere to go
			if current == all_ids[-1]: arrow = "left" # already on the last one

	try:
		press(arrow)
	except Exception as e: # e.g. pyautogui's fail-safe when the mouse is in a screen corner
		log.warning("keypress failed: %s", e)
		return None

	landed = None # desktop we ended up on; None = can't tell
	def switch_back() -> None:
		now = _desktops()
		if landed is not None and now is not None and now[0] != landed:
			return # the user has moved on by themselves since: leave them there
		if not _switch_desktop(press, "left" if arrow == "right" else "right"):
			log.warning("couldn't switch back to the original desktop")

	if desktops is None: return switch_back # can't verify -> assume it worked

	deadline = time.monotonic() + _SWITCH_TIMEOUT
	while time.monotonic() < deadline:
		now = _desktops()
		if now is None: return switch_back
		if now[0] != desktops[0]:
			landed = now[0]
			return switch_back
		time.sleep(0.02)
	return None

_skylight = None
def _desktops() -> tuple[int, list[int]]|None:
	'''
	(current desktop ID, IDs of all desktops on its display, left to right), or None if we can't
	tell. Private read-only API, so anything going wrong here just means "don't know"
	'''
	global _skylight
	if not IS_MAC: return None
	try:
		import ctypes, objc
		if _skylight is None:
			lib = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
			lib.CGSMainConnectionID.restype = ctypes.c_int
			lib.CGSGetActiveSpace.restype = ctypes.c_uint64
			lib.CGSGetActiveSpace.argtypes = [ctypes.c_int]
			lib.CGSCopyManagedDisplaySpaces.restype = ctypes.c_void_p
			lib.CGSCopyManagedDisplaySpaces.argtypes = [ctypes.c_int]
			_skylight = lib
		connection = _skylight.CGSMainConnectionID()
		current = _skylight.CGSGetActiveSpace(connection)
		for display in objc.objc_object(c_void_p=_skylight.CGSCopyManagedDisplaySpaces(connection)):
			ids = [int(space["id64"]) for space in display["Spaces"]]
			if current in ids: return current, ids
		return None
	except Exception:
		return None

def _hide_front_app() -> Callable[[], None]|None:
	'''Hides the app that owns the frontmost window; returns the undo, or None if there was none'''
	import AppKit, Quartz

	# don't hide ourselves or the detector's preview window (it lives in a child process)
	ours = {os.getpid(), os.getppid()} | {child.pid for child in multiprocessing.active_children()}

	# windows come front-to-back; layer 0 = normal app windows. Asking the window server (rather
	# than NSWorkspace.frontmostApplication) because that goes stale without a run loop
	windows = Quartz.CGWindowListCopyWindowInfo(
		Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
		Quartz.kCGNullWindowID,
	) or []
	for window in windows:
		pid = window.get("kCGWindowOwnerPID")
		if window.get("kCGWindowLayer") != 0 or pid in ours: continue
		if window.get("kCGWindowAlpha", 1) == 0: continue
		app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
		if app is None: continue
		app.hide() # its return value isn't trustworthy: said False while the app did get hidden
		def unhide() -> None:
			app.unhide()
			app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows) # back in front, too
		return unhide
	return None


if __name__ == "__main__":
	# `python src/evader.py` -> says what would be used; add `--go` to actually fire it, then undo it 3s later
	logging.basicConfig(level=logging.INFO, format="%(message)s")
	print("can post keypresses:", can_post_keypresses())
	method = choose_evasion_method()
	print("chosen method:", method.__name__)
	if "--go" in sys.argv:
		print("evading...")
		restore = method()
		print("evaded; restoring in 3s")
		time.sleep(3)
		restore()
		print("restored")
