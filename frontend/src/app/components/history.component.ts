import { Component, OnInit, Pipe, PipeTransform } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api.service';
import {
  AssayVersion, Material, ReconBatchDetail, ReconBatchSummary, ReconEvent,
  RunDetail, RunSummary,
} from '../models/models';

@Pipe({ name: 'diffColor', standalone: true })
export class DiffColorPipe implements PipeTransform {
  transform(v: number | null | undefined): string {
    if (v == null || Math.abs(v) < 1e-9) { return 'inherit'; }
    return v > 0 ? '#1d7a46' : '#b3261e';
  }
}

interface ReceiveForm {
  client_event_id: string;
  plan_item_id: number | null;
  assay_version_id: number | null;
  moisture_pct: number | null;
  basis: 'wet' | 'dry';
  qty: number | null;
  cost_per_t_wet: number | null;
  note: string;
}

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [CommonModule, FormsModule, DiffColorPipe],
  templateUrl: './history.component.html',
})
export class HistoryComponent implements OnInit {
  view: 'runs' | 'recon' = 'runs';
  runs: RunSummary[] = [];
  detail: RunDetail | null = null;
  loading = false;

  batches: ReconBatchSummary[] = [];
  batch: ReconBatchDetail | null = null;
  materials: Material[] = [];
  form: ReceiveForm = this.emptyForm();
  reversing: Record<number, boolean> = {};
  correcting: Record<number, boolean> = {};
  corr: Record<number, Partial<ReceiveForm>> = {};
  busy = false;
  lastError = '';
  lastInfo = '';

  constructor(private api: ApiService) {}

  ngOnInit(): void { this.reloadRuns(); }

  emptyForm(): ReceiveForm {
    return {
      client_event_id: this.genId(), plan_item_id: null, assay_version_id: null,
      moisture_pct: null, basis: 'wet', qty: null, cost_per_t_wet: null,
      note: '',
    };
  }

  genId(): string {
    return 'FE-' + Math.random().toString(36).slice(2, 8) + '-' + Date.now().toString(36);
  }

  // ---- 试算历史 ----
  switchView(v: 'runs' | 'recon'): void {
    this.view = v;
    this.lastError = this.lastInfo = '';
    if (v === 'runs') { this.reloadRuns(); } else { this.reloadBatches(); }
  }

  reloadRuns(): void {
    this.api.runs().subscribe(rs => { this.runs = rs; });
  }

  openRun(id: number): void {
    this.loading = true;
    this.api.run(id).subscribe(d => { this.detail = d; this.loading = false; });
  }

  ind(s: any, key: string): string {
    return s.payload?.indicators?.[key] != null
      ? Number(s.payload.indicators[key]).toFixed(3) : '—';
  }

  createRecon(runId: number, solutionId: number): void {
    this.api.createReconBatch({ run_id: runId, solution_id: solutionId })
      .subscribe({
        next: b => {
          this.view = 'recon';
          this.reloadBatches(() => this.openBatch(b.id));
        },
        error: e => this.showError(e),
      });
  }

  // ---- 回填账本 ----
  reloadBatches(done?: () => void): void {
    this.api.reconBatches().subscribe(bs => {
      this.batches = bs;
      if (done) { done(); }
    });
  }

  openBatch(id: number): void {
    this.ensureMaterials();
    this.api.reconBatch(id).subscribe({
      next: b => {
        this.batch = b;
        this.form = this.emptyForm();
        this.reversing = {};
        this.correcting = {};
        this.corr = {};
      },
      error: e => this.showError(e),
    });
  }

  statusLabel(s: string): string {
    return s === 'reconciled' ? '已对账' : s === 'abnormal' ? '异常' : '待对账';
  }

  statusClass(s: string): string {
    return s === 'reconciled' ? 'ok' : s === 'abnormal' ? 'err' : 'warn';
  }

  eventTypeLabel(t: string): string {
    return t === 'reversal' ? '冲销' : t === 'correction' ? '更正' : '到料';
  }

  materialOf(materialId: number): Material | undefined {
    return this.materials.find(m => m.id === materialId);
  }

  planItem(planItemId: number | null): any | null {
    if (!this.batch || planItemId == null) { return null; }
    return this.batch.plan.items.find((i: any) => i.plan_item_id === planItemId)
      || null;
  }

  onPlanItemPick(): void {
    const pi = this.planItem(this.form.plan_item_id);
    if (!pi) { return; }
    if (this.form.assay_version_id == null) {
      this.form.assay_version_id = pi.assay_version_id;
    }
    if (this.form.moisture_pct == null) {
      this.form.moisture_pct = pi.moisture_pct;
    }
    const mat = this.materialOf(pi.material_id);
    if (this.form.cost_per_t_wet == null && mat) {
      this.form.cost_per_t_wet = mat.cost_per_t_wet;
    }
  }

  submitEvent(): void {
    if (!this.batch || !this.form.plan_item_id || !this.form.assay_version_id
        || this.form.moisture_pct == null || this.form.qty == null) {
      this.lastError = '请选择计划原料项、化验版本，并填写含水率与数量。';
      return;
    }
    const body: any = {
      client_event_id: this.form.client_event_id,
      plan_item_id: this.form.plan_item_id,
      assay_version_id: this.form.assay_version_id,
      moisture_pct: this.form.moisture_pct,
      cost_per_t_wet: this.form.cost_per_t_wet,
      note: this.form.note || null,
    };
    if (this.form.basis === 'wet') { body.mass_t_wet = this.form.qty; }
    else { body.mass_t_dry = this.form.qty; }
    this.busy = true;
    this.api.appendReconEvent(this.batch.id, body).subscribe({
      next: () => this.afterMutation('到料已登记（只追加）。'),
      error: e => this.showError(e),
    });
  }

  toggleReverse(ev: ReconEvent): void {
    this.reversing[ev.id] = !this.reversing[ev.id];
  }

  doReverse(ev: ReconEvent): void {
    if (!this.batch) { return; }
    const body = {
      client_event_id: this.genId(), target_event_id: ev.id, mode: 'reverse' as const,
      note: `前端冲销 #${ev.seq}`,
    };
    this.busy = true;
    this.api.reverseReconEvent(this.batch.id, body).subscribe({
      next: () => this.afterMutation('已生成反向冲销事件，原事件保留留痕。'),
      error: e => this.showError(e),
    });
  }

  toggleCorrect(ev: ReconEvent): void {
    if (!this.correcting[ev.id]) {
      this.corr[ev.id] = {
        client_event_id: this.genId(),
        assay_version_id: ev.assay_version_id,
        moisture_pct: ev.moisture_pct,
        basis: 'wet',
        qty: ev.mass_t_wet,
        cost_per_t_wet: ev.assay_snapshot?.cost_per_t_wet ?? null,
      };
    }
    this.correcting[ev.id] = !this.correcting[ev.id];
  }

  doCorrect(ev: ReconEvent): void {
    if (!this.batch) { return; }
    const f = this.corr[ev.id] as any;
    if (!f.assay_version_id || f.moisture_pct == null || f.qty == null) {
      this.lastError = '更正需选择化验版本、含水率与新数量。';
      return;
    }
    const body: any = {
      client_event_id: f.client_event_id, target_event_id: ev.id,
      mode: 'correct', assay_version_id: f.assay_version_id,
      moisture_pct: f.moisture_pct, cost_per_t_wet: f.cost_per_t_wet,
    };
    if (f.basis === 'wet') { body.mass_t_wet = f.qty; }
    else { body.mass_t_dry = f.qty; }
    this.busy = true;
    this.api.reverseReconEvent(this.batch.id, body).subscribe({
      next: () => this.afterMutation('已追加反向事件 + 更正到料事件。'),
      error: e => this.showError(e),
    });
  }

  closeBatch(): void {
    if (!this.batch) { return; }
    this.busy = true;
    this.api.closeReconBatch(this.batch.id).subscribe({
      next: r => this.afterMutation(`批次状态：${this.statusLabel(r.status)}。`),
      error: e => this.showError(e),
    });
  }

  afterMutation(msg: string): void {
    this.busy = false;
    this.lastError = '';
    this.lastInfo = msg;
    if (this.batch) {
      const id = this.batch.id;
      this.reloadBatches(() => this.openBatch(id));
    }
  }

  showError(e: any): void {
    this.busy = false;
    this.lastInfo = '';
    const err = e?.error;
    this.lastError = err?.error_code
      ? `${err.error_code}：${err.message || ''}`
      : (e?.message || '请求失败。');
  }

  loadMaterials(): void {
    this.api.materials(false).subscribe(ms => { this.materials = ms; });
  }

  ensureMaterials(): void {
    if (!this.materials.length) { this.loadMaterials(); }
  }

  // ---- 模板辅助 ----
  assayOptions(planItemId: number | null): AssayVersion[] {
    const pi = this.planItem(planItemId);
    if (!pi) { return []; }
    return this.materialOf(pi.material_id)?.assay_versions ?? [];
  }

  planTrace(planItemId: number | null): { steps: any[] } | null {
    const pi = this.planItem(planItemId);
    return pi?.conversion_trace ?? null;
  }

  seqOf(eventId: number | null | undefined): number | string {
    if (!this.batch || eventId == null) { return '?'; }
    return this.batch.events.find(e => e.id === eventId)?.seq ?? '?';
  }

  isReversed(ev: ReconEvent): boolean {
    return !!this.batch?.events.some(e => e.reverses_event_id === ev.id
      || e.corrects_event_id === ev.id);
  }

  eventBadgeClass(t: string): string {
    return t === 'reversal' ? 'err' : t === 'correction' ? 'ok' : 'warn';
  }
}
