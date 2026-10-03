import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import {
  BlendRequest, BlendResponse, Material,
  ReconBatchDetail, ReconBatchSummary, RunDetail, RunSummary,
} from '../models/models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = '/api';

  constructor(private http: HttpClient) {}

  materials(activeOnly = false): Observable<Material[]> {
    return this.http.get<Material[]>(`${this.base}/materials`, {
      params: activeOnly ? { active_only: true } : {},
    });
  }

  blend(req: BlendRequest): Observable<BlendResponse> {
    return this.http.post<BlendResponse>(`${this.base}/blend`, req);
  }

  evaluate(picks: { material_id: number; assay_version_id?: number | null }[],
           shares: number[], scenarioName: string): Observable<any> {
    return this.http.post(`${this.base}/evaluate`, {
      scenario_name: scenarioName, picks, shares_pct_dry: shares,
    });
  }

  runs(): Observable<RunSummary[]> {
    return this.http.get<RunSummary[]>(`${this.base}/runs`);
  }

  run(id: number): Observable<RunDetail> {
    return this.http.get<RunDetail>(`${this.base}/runs/${id}`);
  }

  // ---------------- 对账批次回填账本 ----------------

  createReconBatch(runId: number, solutionId: number): Observable<ReconBatchDetail> {
    return this.http.post<ReconBatchDetail>(`${this.base}/recon-batches`, {
      run_id: runId, solution_id: solutionId,
    });
  }

  reconBatches(runId?: number): Observable<ReconBatchSummary[]> {
    return this.http.get<ReconBatchSummary[]>(`${this.base}/recon-batches`, {
      params: runId != null ? { run_id: runId } : {},
    });
  }

  reconBatch(id: number): Observable<ReconBatchDetail> {
    return this.http.get<ReconBatchDetail>(`${this.base}/recon-batches/${id}`);
  }

  appendReconEvent(batchId: number, body: {
    event_id: string; kind: 'receipt' | 'correction';
    blend_item_id: number; assay_version_id: number; mass_t_wet: number;
    reverses_event_id?: number | null; note?: string | null;
  }): Observable<any> {
    return this.http.post(`${this.base}/recon-batches/${batchId}/events`, body);
  }

  reverseReconEvent(batchId: number, eventPk: number, eventId: string,
                    note?: string): Observable<any> {
    return this.http.post(
      `${this.base}/recon-batches/${batchId}/events/${eventPk}/reverse`,
      { event_id: eventId, note: note ?? null },
    );
  }
}
