import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { MdSlowMotionVideo } from "react-icons/md";

// ---------------------------------------------------------------------------------------------
// backend
// ---------------------------------------------------------------------------------------------

// afmf-linux's three options, named after AFMF 2.1 in AMD Software
interface Profile {
  search_mode: "auto" | "standard" | "high";
  performance_mode: "auto" | "quality" | "performance";
  fast_motion: "repeat" | "blend";
  // base frame rate limit (0 = off)
  limit: number;
}

interface LayerStatus {
  installed: boolean;
  version: string | null;
  wrapper: string;
  deck_model: "lcd" | "oled" | null;
  error: string | null;
}

interface GameState {
  appid: string;
  enabled: boolean;
  profile: Profile;
}

interface GameEntry {
  appid: string;
  name: string;
  enabled: boolean;
}

type LiveState =
  | "no_session"
  | "off"
  | "no_live"
  | "starting"
  | "not_loaded"
  | "loaded"
  | "passthrough"
  | "active"
  | "partial"
  | "below_min"
  | "idle"
  | "stale"
  | "repeating"
  | "live_off"
  | "unpaced"
  | "error";

interface Live {
  state: LiveState;
  age?: number;
  layer?: boolean;
  problem?: string | null;
  reason?: string | null;
  resolution?: string;
  real_fps?: number;
  output_fps?: number;
  generated_pct?: number;
  governor_step?: number;
  no_image_pct?: number;
  min_fps?: number;
  limit?: number;
  restart_needed?: boolean;
}

const getStatus = callable<[], LayerStatus>("get_status");
const repairLayer = callable<[], { ok: boolean; error?: string }>("repair_layer");
const getGame = callable<[string], GameState>("get_game");
const setGameEnabled = callable<[string, string, boolean], GameState>("set_game_enabled");
const setGameProfile = callable<[string, Profile], GameState>("set_game_profile");
const resetGameProfile = callable<[string], GameState>("reset_game_profile");
const forgetGame = callable<[string], boolean>("forget_game");
const listGames = callable<[], GameEntry[]>("list_games");
const getLaunchMigration = callable<[], { needed: boolean; old_wrapper_name: string; appids: string[] }>(
  "get_launch_migration",
);
const setLaunchMigrationDone = callable<[], boolean>("set_launch_migration_done");
const getLiveStatus = callable<[string], Live>("get_live_status");

// ---------------------------------------------------------------------------------------------
// Steam launch options
// ---------------------------------------------------------------------------------------------

const SHORTCUT_APP_TYPE = 1073741824;

interface RunningApp {
  appid: string;
  name: string;
  shortcut: boolean;
}

function currentApp(): RunningApp | null {
  const app = Router.MainRunningApp;
  if (!app || !app.appid) return null;
  return {
    appid: String(app.appid),
    name: app.display_name || `App ${app.appid}`,
    shortcut: Number(app.app_type) === SHORTCUT_APP_TYPE,
  };
}

function isShortcut(appid: string): boolean {
  try {
    const ov = appStore.GetAppOverviewByAppID(Number(appid));
    return Number(ov?.app_type) === SHORTCUT_APP_TYPE;
  } catch {
    return false;
  }
}

function readLaunchOptions(appid: string, shortcut: boolean): Promise<string> {
  return new Promise((resolve, reject) => {
    let done = false;
    let reg: { unregister: () => void } | null = null;
    const finish = (value: string | null, err?: string) => {
      if (done) return;
      done = true;
      // the callback may fire synchronously, before `reg` is assigned
      setTimeout(() => reg?.unregister(), 0);
      if (value === null) reject(new Error(err ?? "Could not read launch options"));
      else resolve(value);
    };
    reg = SteamClient.Apps.RegisterForAppDetails(Number(appid), (details) => {
      const opts = shortcut ? details?.strShortcutLaunchOptions : details?.strLaunchOptions;
      finish(opts ?? "");
    });
    setTimeout(() => finish(null, "Timed out reading launch options"), 4000);
  });
}

function writeLaunchOptions(appid: string, shortcut: boolean, value: string) {
  if (shortcut) SteamClient.Apps.SetShortcutLaunchOptions(Number(appid), value);
  else SteamClient.Apps.SetAppLaunchOptions(Number(appid), value);
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function stripWrapper(opts: string, wrapper: string): string {
  // "<wrapper> <appid> " directly in front of %command%, wherever the user put it
  const re = new RegExp(`\\s*${escapeRe(wrapper)}\\s+\\d+(?=\\s+%command%)`, "g");
  let out = opts.replace(re, " ").replace(/\s{2,}/g, " ").trim();
  if (out === "%command%") out = "";
  return out;
}

function addWrapper(opts: string, wrapper: string, appid: string): string {
  const clean = stripWrapper(opts, wrapper);
  const part = `${wrapper} ${appid} %command%`;
  if (clean.includes("%command%")) return clean.replace("%command%", part);
  return clean ? `${part} ${clean}` : part;
}

function hasWrapper(opts: string, wrapper: string): boolean {
  return opts.includes(wrapper);
}

async function applyLaunchOptions(appid: string, wrapper: string, enable: boolean): Promise<boolean> {
  const shortcut = isShortcut(appid);
  const before = await readLaunchOptions(appid, shortcut);
  const next = enable ? addWrapper(before, wrapper, appid) : stripWrapper(before, wrapper);
  if (next !== before) writeLaunchOptions(appid, shortcut, next);
  await new Promise((r) => setTimeout(r, 400));
  const after = await readLaunchOptions(appid, shortcut);
  return hasWrapper(after, wrapper) === enable;
}

// ---------------------------------------------------------------------------------------------
// live status presentation
// ---------------------------------------------------------------------------------------------

type Tone = "green" | "yellow" | "red" | "grey" | "blue";
const TONE_COLOR: Record<Tone, string> = {
  green: "#5ba32b",
  yellow: "#d6a01d",
  red: "#d9453a",
  grey: "#8b929a",
  blue: "#1a9fff",
};

function describe(live: Live | null, enabled: boolean, launchOk: boolean): { tone: Tone; title: string; detail?: string } {
  if (!live) return { tone: "grey", title: "Loading status…" };
  if (live.state === "live_off" || (!enabled && live.state !== "no_session" && live.state !== "off"))
    return {
      tone: "grey",
      title: "Off",
      detail: "Real frames only, no frame limit. Turn it back on to compare – it switches instantly while the game runs.",
    };
  const fps =
    live.real_fps !== undefined ? `${live.real_fps} real FPS → ~${live.output_fps} FPS` : undefined;
  switch (live.state) {
    case "no_session":
      if (enabled && !launchOk)
        return { tone: "red", title: "Launch option missing", detail: "Turn the toggle off and on again." };
      return enabled
        ? { tone: "yellow", title: "Enabled – restart required", detail: "Frame generation loads when the game starts. Quit and relaunch it." }
        : { tone: "grey", title: "Off" };
    case "off":
      return enabled
        ? { tone: "yellow", title: "Enabled – restart required", detail: "This session was started without frame generation." }
        : { tone: "grey", title: "Off", detail: "Will not load on the next launch." };
    case "no_live":
      return { tone: "blue", title: "Loaded", detail: "No statistics from the layer for this session." };
    case "starting":
      return { tone: "grey", title: "Game starting…" };
    case "not_loaded":
      return {
        tone: "red",
        title: "Layer not loaded",
        detail: "No output from afmf-linux. Likely causes: layer not installed, 32-bit game, or a game that does not render with Vulkan (e.g. OpenGL).",
      };
    case "loaded":
      return { tone: "yellow", title: "Layer loaded – waiting for frames…" };
    case "passthrough":
      return { tone: "red", title: "Pass-through – no frame generation", detail: live.reason ? `Reason: ${live.reason}` : undefined };
    case "active":
      return { tone: "green", title: "Active – generating frames", detail: fps };
    case "partial": {
      let why = "";
      if ((live.governor_step ?? 0) >= 2) why = ` GPU busy: only every ${live.governor_step}. frame gets a generated frame.`;
      else if ((live.no_image_pct ?? 0) > 20) why = " Not enough free swapchain images.";
      return { tone: "yellow", title: `Partially active (${live.generated_pct}%)`, detail: (fps ?? "") + "." + why };
    }
    case "below_min":
      return {
        tone: "yellow",
        title: "Paused – frame rate too low",
        detail: `${live.real_fps} real FPS is below ${live.min_fps}. Frame generation resumes once the game runs faster.`,
      };
    case "idle":
      return {
        tone: "yellow",
        title: "Loaded, but no generated frames",
        detail: (live.no_image_pct ?? 0) > 20 ? "No free swapchain images for generated frames." : fps,
      };
    case "stale":
      return { tone: "grey", title: "No new frames", detail: "Game paused, in a menu or in the background." };
    case "unpaced":
      return {
        tone: "red",
        title: "Generating frames, but not paced",
        detail:
          "Generated frames reach the screen together with the real ones, so motion is not smoother – only latency rises. Please report this game with its log.",
      };
    case "repeating":
      return {
        tone: "red",
        title: "Repeating frames only",
        detail:
          "The layer cannot interpolate here and only shows frames twice, so motion is not smoother." +
          (live.problem ? ` (${live.problem})` : ""),
      };
    default:
      return { tone: "red", title: "Could not read status", detail: live.problem ?? undefined };
  }
}

function GameLine({ app, live, enabled, launchOk }: { app: RunningApp; live: Live | null; enabled: boolean; launchOk: boolean }) {
  const d = describe(live, enabled, launchOk);
  const restart = enabled && live?.restart_needed && live.state !== "no_session" && live.state !== "off";
  return (
    <div>
      <div style={{ fontWeight: "bold", color: "#dcdedf" }}>
        {app.name}
        {app.shortcut && <span style={{ opacity: 0.6, fontWeight: "normal" }}> (Non-Steam)</span>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "6px", marginTop: "2px" }}>
        <span
          style={{
            width: "8px",
            height: "8px",
            borderRadius: "50%",
            background: TONE_COLOR[d.tone],
            boxShadow: d.tone === "green" ? `0 0 6px ${TONE_COLOR.green}` : undefined,
            flexShrink: 0,
          }}
        />
        <span>{d.title}</span>
      </div>
      {d.detail && <div style={{ opacity: 0.8 }}>{d.detail}</div>}
      {restart && <div style={{ color: TONE_COLOR.yellow }}>Restart the game to apply your changes.</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// settings (afmf-linux's options, named after AFMF 2.1)
// ---------------------------------------------------------------------------------------------

function limitText(v: number): string {
  if (v <= 0) return "Off – the game's real (base) frames are not limited. Applies instantly.";
  return `Limits the game's real (base) frames to ${v} FPS. Frame generation doubles them: ${v} → ${v * 2} FPS on screen. Applies instantly, only while Smooth Frames is on.`;
}

function LimitControl({ value, fallback, onChange }: { value: number; fallback: number; onChange: (v: number) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  const enabled = v > 0;
  return (
    <>
      <PanelSectionRow>
        <ToggleField
          label="Frame Rate Limit"
          description={limitText(v)}
          checked={enabled}
          onChange={(on) => {
            const next = on ? fallback : 0;
            setV(next);
            onChange(next);
          }}
        />
      </PanelSectionRow>
      {enabled && (
        <PanelSectionRow>
          <SliderField
            label="Base FPS"
            value={v}
            min={10}
            max={120}
            step={1}
            showValue={true}
            editableValue={true}
            valueSuffix=" FPS"
            onChange={(n) => {
              setV(n);
              onChange(n);
            }}
          />
        </PanelSectionRow>
      )}
    </>
  );
}

function GameSettings({
  profile,
  onChange,
  onReset,
  deck,
}: {
  profile: Profile;
  onChange: (p: Profile) => void;
  onReset: () => void;
  deck: LayerStatus["deck_model"];
}) {
  const [p, setP] = useState<Profile>(profile);
  const [showOptions, setShowOptions] = useState(false);
  useEffect(() => setP(profile), [profile]);
  const save = useDebounced<Profile>(onChange, 350);
  const set = <K extends keyof Profile>(k: K, v: Profile[K]) => {
    const next = { ...p, [k]: v };
    setP(next);
    save(next);
  };
  // half the built-in refresh rate when the limit is switched back on
  const fallback = deck === "oled" ? 45 : 30;

  return (
    <>
      <LimitControl value={p.limit} fallback={fallback} onChange={(v) => set("limit", v)} />
      <PanelSectionRow>
        <ToggleField label="Frame Generation Options" checked={showOptions} onChange={setShowOptions} />
      </PanelSectionRow>
      {showOptions && (
        <>
          <PanelSectionRow>
            <DropdownItem
              label="Search Mode"
              description="How far motion is searched. High helps with fast camera pans but costs more. Auto: Standard with Performance Mode, High with Quality."
              rgOptions={[
                { data: "auto", label: "Auto" },
                { data: "standard", label: "Standard" },
                { data: "high", label: "High" },
              ]}
              selectedOption={p.search_mode}
              onChange={(o) => set("search_mode", o.data)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <DropdownItem
              label="Performance Mode"
              description="Motion is estimated at half (Performance) or full (Quality) resolution. Auto uses Performance on the Steam Deck."
              rgOptions={[
                { data: "auto", label: "Auto" },
                { data: "quality", label: "Quality" },
                { data: "performance", label: "Performance" },
              ]}
              selectedOption={p.performance_mode}
              onChange={(o) => set("performance_mode", o.data)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <DropdownItem
              label="Fast Motion Response"
              description="Where motion is uncertain: Repeat Frame shows the previous frame there, Blended Frame mixes both frames."
              rgOptions={[
                { data: "repeat", label: "Repeat Frame" },
                { data: "blend", label: "Blended Frame" },
              ]}
              selectedOption={p.fast_motion}
              onChange={(o) => set("fast_motion", o.data)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={onReset}>
              Reset to defaults
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </>
  );
}

function useDebounced<T>(fn: (v: T) => void, ms: number) {
  const t = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latest = useRef(fn);
  latest.current = fn;
  return useCallback(
    (v: T) => {
      if (t.current) clearTimeout(t.current);
      t.current = setTimeout(() => latest.current(v), ms);
    },
    [ms],
  );
}

// ---------------------------------------------------------------------------------------------
// main panel
// ---------------------------------------------------------------------------------------------

function Content() {
  const [status, setStatus] = useState<LayerStatus | null>(null);
  const [app, setApp] = useState<RunningApp | null>(currentApp());
  const [game, setGame] = useState<GameState | null>(null);
  const [launchOk, setLaunchOk] = useState(true);
  const [toggling, setToggling] = useState(false);
  const [live, setLive] = useState<Live | null>(null);
  const [games, setGames] = useState<GameEntry[]>([]);
  const enabledCount = games.filter((g) => g.enabled).length;
  const [cleaning, setCleaning] = useState(false);

  const refreshStatus = useCallback(async () => setStatus(await getStatus()), []);
  const refreshGames = useCallback(async () => setGames(await listGames()), []);

  useEffect(() => {
    refreshStatus();
    refreshGames();
  }, []);

  // follow the running game
  useEffect(() => {
    const id = setInterval(() => {
      const a = currentApp();
      setApp((prev) => (prev?.appid === a?.appid ? prev : a));
    }, 1500);
    return () => clearInterval(id);
  }, []);

  // the running game's settings, and whether its launch option is in place
  useEffect(() => {
    setGame(null);
    setLive(null);
    if (!app) return;
    let cancelled = false;
    (async () => {
      const g = await getGame(app.appid);
      if (cancelled) return;
      setGame(g);
      if (g.enabled && status) {
        try {
          const opts = await readLaunchOptions(app.appid, app.shortcut);
          if (!cancelled) setLaunchOk(hasWrapper(opts, status.wrapper));
        } catch {
          /* keep the last value */
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [app?.appid, status?.wrapper]);

  // live status every 2 s while the panel is open
  useEffect(() => {
    if (!app) return;
    let stop = false;
    const tick = async () => {
      try {
        const l = await getLiveStatus(app.appid);
        if (!stop) setLive(l);
      } catch {
        /* ignore */
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [app?.appid]);

  const toggleGame = async (on: boolean) => {
    if (!app || !status) return;
    setToggling(true);
    try {
      const ok = await applyLaunchOptions(app.appid, status.wrapper, on);
      setGame(await setGameEnabled(app.appid, app.name, on));
      // the layer re-reads its control file within 250 ms
      setTimeout(async () => setLive(await getLiveStatus(app.appid)), 400);
      setLaunchOk(on ? ok : true);
      if (on && !ok) toaster.toast({ title: "Smooth Frames", body: "Could not set the launch option." });
      refreshGames();
    } catch (e) {
      toaster.toast({ title: "Smooth Frames", body: String(e) });
    } finally {
      setToggling(false);
    }
  };

  const removeAll = async () => {
    if (!status) return;
    setCleaning(true);
    for (const g of games) {
      try {
        await applyLaunchOptions(g.appid, status.wrapper, false);
      } catch {
        /* app may be uninstalled: forget it anyway */
      }
      await forgetGame(g.appid);
    }
    setCleaning(false);
    if (app) setGame(await getGame(app.appid));
    refreshGames();
    toaster.toast({ title: "Smooth Frames", body: "Smooth Frames removed from all games." });
  };

  const doRepair = async () => {
    const r = await repairLayer();
    toaster.toast({ title: "Smooth Frames", body: r.ok ? "Frame generation layer reinstalled." : `Failed: ${r.error}` });
    await refreshStatus();
  };

  const layerReady = !!status?.installed;
  const enabled = !!game?.enabled;

  return (
    <>
      <PanelSection>
        {status && !layerReady && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              label="Frame generation layer missing"
              description={status.error ?? "The plugin could not set up its layer."}
              onClick={doRepair}
            >
              Repair
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Smooth Frames"
            description={
              app ? (
                <GameLine app={app} live={layerReady ? live : null} enabled={enabled} launchOk={launchOk} />
              ) : (
                "Start a game to turn Smooth Frames on for it."
              )
            }
            checked={enabled}
            disabled={!app || !layerReady || toggling || !game}
            onChange={toggleGame}
          />
        </PanelSectionRow>
        {app && game?.enabled && (
          <GameSettings
            deck={status?.deck_model ?? null}
            profile={game.profile}
            onChange={async (p) => setGame(await setGameProfile(app.appid, p))}
            onReset={async () => setGame(await resetGameProfile(app.appid))}
          />
        )}
      </PanelSection>

      {enabledCount > 0 && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={cleaning}
              description={`Smooth Frames is on in ${enabledCount} game${enabledCount === 1 ? "" : "s"}. This turns it off everywhere and cleans up the launch options – do this before uninstalling the plugin.`}
              onClick={removeAll}
            >
              {cleaning ? "Removing…" : "Remove Smooth Frames from all games"}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}

// Games enabled under the plugin's earlier name (Decky-AFMF) call its wrapper: point them at ours.
async function migrateLaunchOptions() {
  try {
    const m = await getLaunchMigration();
    if (!m.needed) return;
    const status = await getStatus();
    const old = new RegExp(`\\S*/${escapeRe(m.old_wrapper_name)}\\s+\\d+(?=\\s+%command%)`, "g");
    for (const appid of m.appids) {
      try {
        const shortcut = isShortcut(appid);
        const opts = await readLaunchOptions(appid, shortcut);
        if (!opts.includes(m.old_wrapper_name)) continue;
        const cleaned = opts.replace(old, " ").replace(/\s{2,}/g, " ").trim();
        writeLaunchOptions(appid, shortcut, addWrapper(cleaned, status.wrapper, appid));
      } catch {
        /* app no longer installed */
      }
    }
    await setLaunchMigrationDone();
  } catch (e) {
    console.error("Smooth Frames: launch option migration failed", e);
  }
}

export default definePlugin(() => {
  migrateLaunchOptions();
  return {
  name: "Decky Smooth Frames",
  titleView: <div className={staticClasses.Title}>Smooth Frames</div>,
  content: <Content />,
  icon: <MdSlowMotionVideo />,
  onDismount() {},
  };
});
