import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import {
  BlendRequest, BlendResponse, Material, ReconBatchDetail, ReconBatchSummary,
  ReconEventInput, ReconReverseInput, RunDetail, RunSummary,
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

  // ---- 离线试验回填账本 ----
  reconBatches(): Observable<ReconBatchSummary[]> {
    return this.http.get<ReconBatchSummary[]>(`${this.base}/recon/batches`);
  }

  reconBatch(id: number): Observable<ReconBatchDetail> {
    return this.http.get<ReconBatchDetail>(`${this.base}/recon/batches/${id}`);
  }

  createReconBatch(body: {
    run_id: number; solution_id: number; batch_code?: string;
    tolerance_pct?: number; abs_tol_t?: number; remark?: string;
  }): Observable<ReconBatchDetail> {
    return this.http.post<ReconBatchDetail>(`${this.base}/recon/batches`, body);
  }

  appendReconEvent(batchId: number, body: ReconEventInput): Observable<any> {
    return this.http.post(`${this.base}/recon/batches/${batchId}/events`, body);
  }

  reverseReconEvent(batchId: number, body: ReconReverseInput): Observable<any> {
    return this.http.post(`${this.base}/recon/batches/${batchId}/reverse`, body);
  }

  closeReconBatch(batchId: number, force = false): Observable<any> {
    return this.http.post(`${this.base}/recon/batches/${batchId}/close`,
      { force });
  }
}
