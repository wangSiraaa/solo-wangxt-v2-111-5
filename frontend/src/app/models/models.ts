export interface AssayVersion {
  id: number;
  version: string;
  lab_report_no: string;
  assayed_at: string;
  basis: 'dry' | 'wet';
  composition: Record<string, number>;
  measured_oxides: string[];
}

export interface Material {
  id: number;
  code: string;
  name: string;
  category: string;
  moisture_pct: number;
  cost_per_t_wet: number;
  availability_t_wet: number | null;
  min_share_pct: number;
  is_active: boolean;
  note?: string | null;
  assay_versions: AssayVersion[];
}

export interface Interval { min?: number | null; max?: number | null; }
export interface Targets { SM: Interval; IM: Interval; KH: Interval; }

export interface BlendRequest {
  scenario_name: string;
  batch_t_dry: number;
  candidates: { material_id: number; assay_version_id?: number | null }[];
  targets: Targets;
  hazard_limits_pct: Record<string, number>;
  modes: string[];
  cheap_material_id?: number | null;
  save?: boolean;
}

export interface ConversionStep {
  component: string;
  basis_in: string;
  value_in: number;
  formula: string;
  factor: number;
  basis_out: string;
  value_out: number;
}

export interface SolutionItem {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  share_pct_dry: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  cost: number;
  conversion_trace: {
    material_code: string;
    material_name: string;
    moisture_pct: number;
    assay_basis: string;
    dry_factor: number;
    steps: ConversionStep[];
    mass_balance?: any;
  };
}

export interface Conflict {
  constraint: string;
  limit?: number;
  achieved?: number;
  normalized_gap: number;
}

export interface Solution {
  mode: string;
  mode_label: string;
  success: boolean;
  total_cost?: number;
  cost_per_t_dry?: number;
  indicators?: {
    SM: number; IM: number; KH: number;
    CaO: number; SiO2: number; Al2O3: number; Fe2O3: number;
    warnings: string[];
  };
  composition_dry_pct?: Record<string, number>;
  composition_wet_pct?: Record<string, number>;
  water_pct_in_wet_mix?: number;
  items: SolutionItem[];
  diagnostic?: {
    reason: string; message: string;
    conflicts: Conflict[];
    min_violation_objective?: number;
  };
}

export interface BlendResponse {
  run_id: number | null;
  run_code: string;
  status: string;
  solutions: Solution[];
}

export interface RunSummary {
  id: number; run_code: string; scenario_name: string;
  status: string; created_at: string; modes: string[];
}

export interface RunDetail {
  id: number; run_code: string; scenario_name: string;
  batch_t_dry: number; target: Targets; constraint_set: any;
  status: string; created_at: string;
  solutions: { id: number; mode: string; success: boolean;
               total_cost: number | null; payload: any;
               diagnostic: any; items: any[] }[];
}

// ---------- 离线试验回填账本 ----------
export interface ReconBatchSummary {
  id: number;
  batch_code: string;
  run_id: number;
  solution_id: number;
  status: 'pending' | 'reconciled' | 'abnormal';
  scenario_name: string;
  event_count: number;
  quantity_closed: boolean;
  created_at: string;
  updated_at: string;
}

export interface ReconEventInput {
  client_event_id: string;
  plan_item_id: number;
  assay_version_id: number;
  moisture_pct: number;
  mass_t_wet?: number | null;
  mass_t_dry?: number | null;
  cost_per_t_wet?: number | null;
  occurred_at?: string | null;
  note?: string | null;
}

export interface ReconReverseInput {
  client_event_id: string;
  target_event_id: number;
  mode: 'reverse' | 'correct';
  assay_version_id?: number | null;
  moisture_pct?: number | null;
  mass_t_wet?: number | null;
  mass_t_dry?: number | null;
  cost_per_t_wet?: number | null;
  note?: string | null;
}

export interface ReconEvent {
  id: number;
  seq: number;
  client_event_id: string;
  type: 'receive' | 'reversal' | 'correction';
  sign: number;
  plan_item_id: number;
  material_id: number;
  assay_version_id: number;
  moisture_pct: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  cost: number;
  signed_mass_t_dry: number;
  signed_cost: number;
  assay_snapshot: any;
  reverses_event_id: number | null;
  corrects_event_id: number | null;
  note: string | null;
  occurred_at: string;
  created_at: string;
}

export interface ReconDiffItem {
  plan_item_id: number;
  material_id: number;
  material_code: string;
  material_name: string;
  plan_assay_version: string;
  plan_lab_report_no: string;
  plan_assay_basis: 'dry' | 'wet';
  actual_assay_versions: string[];
  plan: { mass_t_dry: number; mass_t_wet: number; water_t: number;
          cost: number; share_pct_dry: number; moisture_pct: number };
  actual: { mass_t_dry: number; mass_t_wet: number; water_t: number; cost: number };
  diff: { mass_t_dry: number; mass_t_wet: number; water_t: number; cost: number };
  dry_tolerance_t: number;
  closed: boolean;
  event_count: number;
}

export interface ReconIndicatorCheck {
  indicator: string;
  min: number | null; max: number | null;
  plan: number | null; actual: number | null;
  diff: number | null;
  out_of_range: boolean;
}

export interface ReconHazardCheck extends ReconIndicatorCheck {
  hazard: string;
  limit: number;
}

export interface ReconDiff {
  totals: {
    plan: Record<string, number>;
    actual: Record<string, number>;
    diff: Record<string, number>;
  };
  items: ReconDiffItem[];
  actual_composition_dry_pct: Record<string, number> | null;
  plan_composition_dry_pct: Record<string, number> | null;
  indicators: ReconIndicatorCheck[];
  hazards: ReconHazardCheck[];
  quantity_closed: boolean;
  in_range: boolean;
  hard_issue: boolean;
  issues: { code: string; message: string; [k: string]: any }[];
  event_count: number;
  computed_at: string;
}

export interface ReconBatchDetail {
  id: number;
  batch_code: string;
  run_id: number;
  solution_id: number;
  status: 'pending' | 'reconciled' | 'abnormal';
  tolerance_pct: number;
  abs_tol_t: number;
  remark: string | null;
  created_at: string;
  updated_at: string;
  plan: any;
  diff: ReconDiff | null;
  events: ReconEvent[];
}
