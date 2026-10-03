import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api.service';
import {
  Material, ReconBatchDetail, ReconBatchSummary, ReconEvent,
  ReconPlanItem, RunDetail, RunSummary,
} from '../models/models';

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './history.component.html',
})
export class HistoryComponent implements OnInit {
  runs: RunSummary[] = [];
  detail: RunDetail | null = null;
  loading = false;

  // ---- 对账批次 ----
  batches: ReconBatchSummary[] = [];
  batch: ReconBatchDetail | null = null;
  materials: Material[] = [];
  err = '';

  // 追加事件表单（到料 / 更正）
  form = {
    blend_item_id: null as number | null,
    assay_version_id: null as number | null,
    mass_t_wet: null as number | null,
    event_id: '',
    note: '',
  };
  correctionTarget: ReconEvent | null = null;  // 非空表示当前为“更正”模式

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.reload();
    this.api.materials().subscribe(ms => { this.materials = ms; });
  }

  reload(): void {
    this.api.runs().subscribe(rs => { this.runs = rs; });
  }

  open(id: number): void {
    this.loading = true;
    this.batch = null;
    this.api.run(id).subscribe(d => {
      this.detail = d; this.loading = false;
      this.api.reconBatches(id).subscribe(bs => { this.batches = bs; });
    });
  }

  ind(s: any, key: string): string {
    return s.payload?.indicators?.[key] != null
      ? Number(s.payload.indicators[key]).toFixed(3) : '—';
  }

  // ---------------- 对账批次操作 ----------------

  createBatch(solutionId: number): void {
    if (!this.detail) { return; }
    this.err = '';
    this.api.createReconBatch(this.detail.id, solutionId).subscribe({
      next: b => {
        this.batch = b;
        this.api.reconBatches(this.detail!.id).subscribe(bs => { this.batches = bs; });
        this.resetForm();
      },
      error: e => { this.err = e.error?.message || '创建对账批次失败'; },
    });
  }

  openBatch(id: number): void {
    this.err = '';
    this.api.reconBatch(id).subscribe(b => { this.batch = b; this.resetForm(); });
  }

  statusLabel(s: string): string {
    return { pending: '待对账', reconciled: '已对账', exception: '异常' }[s] ?? s;
  }

  kindLabel(k: string): string {
    return { receipt: '到料', reversal: '冲销', correction: '更正' }[k] ?? k;
  }

  planItem(id: number | null): ReconPlanItem | null {
    if (!this.batch || id == null) { return null; }
    return this.batch.plan.items.find(i => i.blend_item_id === id) ?? null;
  }

  assayOptions(): { id: number; label: string }[] {
    const it = this.planItem(this.form.blend_item_id);
    if (!it) { return []; }
    const mat = this.materials.find(m => m.id === it.material_id);
    return (mat?.assay_versions ?? []).map(v => ({
      id: v.id,
      label: `${v.version} · ${v.lab_report_no} · ${v.basis}`,
    }));
  }

  onItemChange(): void {
    const it = this.planItem(this.form.blend_item_id);
    if (!it) { return; }
    // 默认带出计划化验版，但用户必须显式确认/改选——绝不静默用最新化验单
    this.form.assay_version_id = it.assay_version_id;
    this.form.mass_t_wet = it.mass_t_wet;
  }

  newEventId(): string {
    return (crypto as any).randomUUID?.() ??
      `evt-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  resetForm(): void {
    this.form = {
      blend_item_id: null, assay_version_id: null,
      mass_t_wet: null, event_id: this.newEventId(), note: '',
    };
    this.correctionTarget = null;
  }

  startCorrection(ev: ReconEvent): void {
    this.correctionTarget = ev;
    this.form = {
      blend_item_id: ev.blend_item_id,
      assay_version_id: ev.assay_version_id,
      mass_t_wet: ev.mass_t_wet,
      event_id: this.newEventId(),
      note: `更正事件 ${ev.event_id}`,
    };
  }

  submitEvent(): void {
    if (!this.batch || this.form.blend_item_id == null
        || this.form.assay_version_id == null || !this.form.mass_t_wet) {
      this.err = '请选择计划原料项、化验版本并填写湿基到料吨。';
      return;
    }
    this.err = '';
    const body: any = {
      event_id: this.form.event_id,
      kind: this.correctionTarget ? 'correction' : 'receipt',
      blend_item_id: this.form.blend_item_id,
      assay_version_id: this.form.assay_version_id,
      mass_t_wet: this.form.mass_t_wet,
      note: this.form.note || null,
    };
    if (this.correctionTarget) {
      body.reverses_event_id = this.correctionTarget.id;
    }
    this.api.appendReconEvent(this.batch.id, body).subscribe({
      next: () => { this.openBatch(this.batch!.id); },
      error: e => {
        this.err = e.error?.message
          ? `${e.error.error_code ?? ''} ${e.error.message}` : '入账失败';
      },
    });
  }

  reverse(ev: ReconEvent): void {
    if (!this.batch) { return; }
    this.err = '';
    this.api.reverseReconEvent(
      this.batch.id, ev.id, this.newEventId(), `冲销 ${ev.event_id}`,
    ).subscribe({
      next: () => { this.openBatch(this.batch!.id); },
      error: e => {
        this.err = e.error?.message
          ? `${e.error.error_code ?? ''} ${e.error.message}` : '冲销失败';
      },
    });
  }

  reversible(ev: ReconEvent): boolean {
    return ev.kind !== 'reversal' && !ev.reversed_by_event_id;
  }

  num(v: any, digits = 2): string {
    return v == null ? '—' : Number(v).toFixed(digits);
  }

  hasHazardLimits(): boolean {
    return !!this.batch
      && Object.keys(this.batch.plan.hazard_limits_pct || {}).length > 0;
  }
}
