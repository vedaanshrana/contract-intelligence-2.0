/** Static UI constants (no mock data). */

/** Suggested chat starters shown on an empty conversation. */
export const SUGGESTIONS = [
  'What is the current active MSA for the client(s) selected?',
  'List the active fee line items and their material codes?',
  'What are the CPI escalation terms and termination clauses?',
]

/**
 * The 9 user-facing data cubes, in pipeline order. Mirrors the backend.
 * `metricKey` is the name used in run_metrics.json (see
 * agent_runner._metric_meta) so the UI can look up per-data-cube metrics.
 */
export const FRONTEND_AGENTS: { key: string; display: string; metricKey: string; blurb: string }[] = [
  { key: 'contract_hierarchy', display: 'Hierarchy Data Cube', metricKey: 'hierarchy', blurb: 'Document types, dates, parties, and how amendments relate to the master agreement' },
  { key: 'contract_scope', display: 'Engagement Overview Data Cube', metricKey: 'engagement_overview', blurb: 'Per-contract addresses, signatories, document type, and a plain-English summary' },
  { key: 'product_module', display: 'Product Module Data Cube', metricKey: 'product_module', blurb: 'Products, schedules, and modules within each contract' },
  { key: 'fee_digitization', display: 'Fee Description Data Cube', metricKey: 'extraction', blurb: 'Every fee line item with price, checkbox state, page, and section header' },
  { key: 'material_match', display: 'Material Code Matching Data Cube', metricKey: 'material_match', blurb: 'The SAP material code matched to each billable line item' },
  { key: 'material_validation', display: 'Invoice validated material data cube', metricKey: 'material_validation', blurb: 'Re-scores matched material codes against historical Snowflake invoice data with confidence bands' },
  { key: 'cpi_terms', display: 'CPI Terms Data Cube', metricKey: 'cpi', blurb: 'Annual escalation terms — eligibility dates, floors/caps, notice requirements' },
  { key: 'termination_clause', display: 'Termination Clause Data Cube', metricKey: 'termination', blurb: 'For-cause vs. for-convenience termination, notice periods, early-termination fees' },
  { key: 'mnr_template', display: 'MNR Template Data Cube', metricKey: 'mnr_template', blurb: 'Forensic extraction + material matching, producing a SAP-ready MNR Excel draft with biller colour-coding' },
]

export const AGENT_DISPLAY: Record<string, string> = Object.fromEntries(
  FRONTEND_AGENTS.map((a) => [a.key, a.display]),
)
