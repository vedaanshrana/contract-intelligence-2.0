import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Check, Circle, Play, Square } from 'lucide-react'
import { runAgentStream } from '../../api'
import { FRONTEND_AGENTS } from '../../constants'
import type { ClientStatus, PipelineEvent } from '../../types'

/** Per-agent run state tracked locally for this session (overlays the
 * persisted done flags from the backend in client.agents). */
type LiveStatus = 'idle' | 'queued' | 'running' | 'done' | 'error'

/**
 * Expandable panel that lets the user run individual agents (or any selected
 * subset) for ONE client — independent of the full-pipeline "Run All". Each
 * agent maps to the backend's POST /api/agents/{key}/run (always re-runs,
 * ignoring cache), streamed via runAgentStream and executed sequentially so the
 * backend's per-agent metering stays correct.
 */
export default function AgentRunPanel({
  client,
  core,
  disabled,
  onComplete,
}: {
  client: ClientStatus
  core: string
  /** When the full pipeline is running/queued for this client, lock the panel. */
  disabled: boolean
  onComplete: () => void
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [live, setLive] = useState<Record<string, LiveStatus>>({})
  // Human-readable reason for the latest run of each agent (the backend's
  // `summary` / error message). Surfaced inline + on hover so a failed/skipped
  // agent tells the user WHY, e.g. "skipped — no master agreement found".
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [runningKey, setRunningKey] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [logLine, setLogLine] = useState('')
  const abortRef = useRef<null | (() => void)>(null)

  // Abort any in-flight stream if the panel unmounts (row collapsed / nav away).
  useEffect(() => () => abortRef.current?.(), [])

  const statusFor = useCallback(
    (key: string): LiveStatus => {
      const l = live[key]
      if (l && l !== 'idle') return l
      return client.agents[key] ? 'done' : 'idle'
    },
    [live, client.agents],
  )

  const runKeys = useCallback(
    (keys: string[]) => {
      if (busy || disabled || keys.length === 0) return
      setBusy(true)
      setLogLine('')
      setLive((s) => {
        const n = { ...s }
        keys.forEach((k) => (n[k] = 'queued'))
        return n
      })
      // Clear any stale reason for the agents about to re-run.
      setReasons((r) => {
        const n = { ...r }
        keys.forEach((k) => delete n[k])
        return n
      })

      let i = 0
      const runNext = () => {
        if (i >= keys.length) {
          setBusy(false)
          setRunningKey(null)
          abortRef.current = null
          onComplete() // refresh persisted status + portfolio KPIs
          return
        }
        const key = keys[i++]
        setRunningKey(key)
        setLive((s) => ({ ...s, [key]: 'running' }))
        abortRef.current = runAgentStream(
          key,
          client.client,
          core,
          (ev: PipelineEvent) => {
            if (ev.type === 'log' && ev.message) {
              setLogLine(ev.message)
            } else if (ev.type === 'agent_done') {
              const ok = ev.status === 'complete' || ev.status === 'cached'
              setLive((s) => ({ ...s, [key]: ok ? 'done' : 'error' }))
              // Capture WHY: the backend's human-readable summary (e.g.
              // "skipped — no master agreement found — run Hierarchy first"),
              // else the raw error, else the bare status code.
              const resErr =
                ev.result && typeof ev.result.error === 'string' ? ev.result.error : ''
              const why =
                (ev.summary && ev.summary.trim()) ||
                resErr ||
                (ev.status ? `status: ${ev.status}` : '')
              setReasons((r) => ({ ...r, [key]: why }))
            }
          },
          () => runNext(), // stream closed → next agent
          () => {
            // stream-level error → mark this one failed and keep going
            setLive((s) => ({ ...s, [key]: 'error' }))
            setReasons((r) => ({
              ...r,
              [key]:
                'The agent stream ended unexpectedly (backend error or lost connection). Check the backend terminal/logs for the traceback.',
            }))
            runNext()
          },
        )
      }
      runNext()
    },
    [busy, disabled, client.client, core, onComplete],
  )

  const cancel = useCallback(() => {
    abortRef.current?.()
    abortRef.current = null
    setBusy(false)
    setRunningKey(null)
    setLive((s) => {
      const n = { ...s }
      for (const k of Object.keys(n)) if (n[k] === 'queued' || n[k] === 'running') n[k] = 'idle'
      return n
    })
    onComplete()
  }, [onComplete])

  const toggle = (key: string) =>
    setSelected((s) => {
      const n = new Set(s)
      n.has(key) ? n.delete(key) : n.add(key)
      return n
    })

  const allKeys = useMemo(() => FRONTEND_AGENTS.map((a) => a.key), [])
  const allSelected = selected.size === allKeys.length
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(allKeys))

  return (
    <div className="border border-line bg-surface">
      {/* header */}
      <div className="flex items-center justify-between border-b border-line bg-surface-2/50 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-ink-3">
            Run individual agents · {client.client}
          </span>
          {busy && runningKey && (
            <span className="font-mono text-[9px] text-primary">
              ▸ {FRONTEND_AGENTS.find((a) => a.key === runningKey)?.display ?? runningKey}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleAll}
            disabled={busy || disabled}
            className="border border-line px-2 py-[3px] font-mono text-[9px] uppercase tracking-wider text-ink-2 transition-colors duration-150 hover:border-primary/50 hover:text-primary disabled:opacity-40"
          >
            {allSelected ? 'Clear' : 'Select all'}
          </button>
          {busy ? (
            <button
              onClick={cancel}
              className="flex items-center gap-1.5 bg-warn px-3 py-[3px] font-mono text-[9px] font-semibold uppercase tracking-wider text-white transition-all duration-150 hover:brightness-110"
            >
              <Square size={9} strokeWidth={3} /> Stop
            </button>
          ) : (
            <button
              onClick={() => runKeys([...selected])}
              disabled={selected.size === 0 || disabled}
              className="flex items-center gap-1.5 bg-primary px-3 py-[3px] font-mono text-[9px] font-semibold uppercase tracking-wider text-white transition-all duration-150 hover:brightness-110 disabled:opacity-30"
            >
              <Play size={9} strokeWidth={3} /> Run selected ({selected.size})
            </button>
          )}
        </div>
      </div>

      {/* agent grid */}
      <div className="grid grid-cols-1 gap-px bg-line sm:grid-cols-2 lg:grid-cols-3">
        {FRONTEND_AGENTS.map((a) => {
          const st = statusFor(a.key)
          const checked = selected.has(a.key)
          const why = reasons[a.key]
          const failed = st === 'error'
          return (
            <div
              key={a.key}
              className="flex items-center gap-2 bg-surface px-3 py-2"
              title={failed && why ? why : a.blurb}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={busy || disabled}
                onChange={() => toggle(a.key)}
                className="h-3 w-3 shrink-0 accent-primary disabled:opacity-40"
              />
              <AgentStatusIcon status={st} reason={why} />
              <div className="min-w-0 flex-1">
                <span className="block truncate text-[11px] text-ink-2">{a.display}</span>
                {failed && why && (
                  <span
                    className="block truncate font-mono text-[9px] leading-tight text-bad"
                    title={why}
                  >
                    {why}
                  </span>
                )}
              </div>
              <button
                onClick={() => runKeys([a.key])}
                disabled={busy || disabled}
                className="flex h-5 w-5 shrink-0 items-center justify-center border border-line text-ink-3 transition-colors duration-150 hover:border-primary/50 hover:text-primary disabled:opacity-30"
                aria-label={`Run ${a.display}`}
                title={`Run ${a.display} now`}
              >
                <Play size={9} />
              </button>
            </div>
          )
        })}
      </div>

      {/* live log line */}
      {busy && (
        <div className="border-t border-line bg-surface-2/40 px-3 py-1.5">
          <span className="block truncate font-mono text-[9.5px] text-ink-3">
            {logLine || 'Working…'}
          </span>
        </div>
      )}
    </div>
  )
}

function AgentStatusIcon({ status, reason }: { status: LiveStatus; reason?: string }) {
  switch (status) {
    case 'done':
      return <Check size={12} strokeWidth={3} className="shrink-0 text-ok" />
    case 'running':
      return (
        <span className="animate-arc inline-block h-[11px] w-[11px] shrink-0 rounded-full border-[1.5px] border-primary/25 border-t-primary" />
      )
    case 'queued':
      return <Circle size={11} className="shrink-0 text-ink-3" />
    case 'error':
      // Hover the sign to read the exact reason the agent didn't complete.
      return (
        <span
          className="inline-flex shrink-0 cursor-help"
          title={reason || 'This agent did not complete successfully.'}
        >
          <AlertCircle size={12} className="text-bad" />
        </span>
      )
    default:
      return <Circle size={11} className="shrink-0 text-line-strong" />
  }
}
