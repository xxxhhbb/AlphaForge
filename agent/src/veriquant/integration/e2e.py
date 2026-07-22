"""Automated offline A-F integration acceptance for VeriQuant.

This module is an integration harness, not a new financial truth service. It
adapts READY_FOR_INTEGRATION packages to the existing deterministic FactorDAG,
PIT, Evidence, promotion, and paper-only shadow authorities.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Mapping

_AGENT_SRC = Path(__file__).resolve().parents[2]
if str(_AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(_AGENT_SRC))

from veriquant.adapters.proposal import ProposalContext, compile_proposal_expression
from veriquant.contracts import ContractValidationError, FailureCode, ResearchState
from veriquant.data import FieldRole, MarketSnapshot, SnapshotRow, canonical_content_hash
from veriquant.evidence import CostModel, EvaluationRow, EvidenceService, ExperimentIdentity, OfficialEvidence
from veriquant.factors import CompiledFactorDAG, FactorResult, execute_factor_dag
from veriquant.llm_desks.api import (
    CompilePITArtifact,
    DeskId,
    DeterministicProposalProvider,
    FactorDraftFill,
    LLMDeskStatus,
    OfficialEvaluationView,
    ReadOnlyToolRegistry,
    RedactedEvidenceSummary,
    ResearchQuestionView,
    ResearchStageController,
    build_default_desk_configs,
    build_default_tools_for_desk,
)
from veriquant.real_research.api import (
    AShareResearchBridge,
    ResearchDatasetBundle,
    build_default_bridge_request,
    build_offline_real_research_fixture,
)
from veriquant.research_memory.api import (
    CitationCritic,
    EvidenceGroundedResearchDAG,
    FrozenDocumentStore,
    FrozenWebFixtureGateway,
    HypothesisSynthesizer,
    ResearchBrief,
    ResearchMemoryStore,
    Retriever,
    SourceKind,
    SourceRequest,
)
from veriquant.shadow_portfolio.api import (
    ChallengerState,
    ChallengerVersion,
    DataVintage,
    DeterministicClock,
    EqualWeightPortfolioConstructor,
    InMemoryApprovedChallengerProvider,
    InMemoryFrozenSnapshotProvider,
    InMemoryMarketPriceProvider,
    MarketPrice,
    PerformanceAttribution,
    ShadowLedger,
    ShadowPortfolioService,
    ShadowRiskPolicy,
    ShadowRunRequest,
    StaticRiskPolicyReader,
)
from veriquant.statistical_promotion.contracts import PromotionState, content_hash
from veriquant.integration.acceptance import probe_optional_capabilities
from veriquant.integration.research_memory import adapt_source_backed_draft
from veriquant.integration.statistical import (
    InMemoryOfficialEvidenceReader,
    PromotionEvaluationRequest,
    build_family_manifest,
    candidate_view_from_official_evidence,
    evaluate_statistical_promotion,
)
from veriquant.integration.workbench import publish_workbench_run


class IntegrationInvariantError(RuntimeError):
    """Raised when an acceptance invariant fails."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IntegrationInvariantError(message)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _bundle_snapshot(bundle: ResearchDatasetBundle) -> MarketSnapshot:
    fields = tuple(bundle.feature_fields)
    rows = tuple(
        SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: row.values[field] for field in fields},
        )
        for row in sorted(bundle.feature_rows, key=lambda item: (item.event_time, item.symbol))
    )
    return MarketSnapshot(
        dataset_id=bundle.dataset_card.dataset_id,
        decision_time=bundle.decision_time,
        start_time=min(row.event_time for row in rows),
        fields=fields,
        rows=rows,
        historical_universe={
            key: tuple(value)
            for key, value in bundle.universe_manifest.historical_universe.items()
            if datetime.fromisoformat(key) <= bundle.decision_time
        },
        coverage_by_field={field: bundle.data_quality_manifest.coverage_by_field[field] for field in fields},
    )


class BundleEvidenceAuthority:
    """One deterministic authority shared by all six Desk controllers."""

    def __init__(self, *, bundle: ResearchDatasetBundle, snapshot: MarketSnapshot, seed: int) -> None:
        self.bundle = bundle
        self.snapshot = snapshot
        self.seed = seed
        self.service = EvidenceService()
        self.cost_model = CostModel(spread_bps=1.0, linear_impact_bps=1.0)
        self._dags: dict[str, CompiledFactorDAG] = {}
        self._results: dict[str, FactorResult] = {}
        self._evidence: dict[tuple[str, str], OfficialEvidence] = {}
        self.visible_calls: list[str] = []
        self._label_by_key = {
            (row.event_time, row.symbol): row for row in bundle.label_rows
        }
        self._feature_by_key = {
            (row.event_time, row.symbol): row for row in bundle.feature_rows
        }

    def register_dag(self, dag: CompiledFactorDAG) -> None:
        self._dags[dag.artifact_hash] = dag

    def compile_and_validate(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        factor: FactorDraftFill,
    ) -> CompilePITArtifact:
        dag = compile_proposal_expression(factor.expression, field_roles={field: FieldRole.FEATURE for field in self.snapshot.fields})
        if set(dag.required_fields) != set(factor.variables):
            raise ContractValidationError(
                FailureCode.INVALID_FACTOR_DAG,
                "Desk variables do not match the restricted FactorDAG",
                details={"desk_id": desk_id.value},
            )
        self.register_dag(dag)
        self._factor_result(dag.artifact_hash)
        return CompilePITArtifact(
            compiled_factor_hash=dag.artifact_hash,
            pit_snapshot_hash=self.snapshot.artifact_hash,
        )

    def evaluate_official(
        self,
        *,
        desk_id: DeskId,
        question: ResearchQuestionView,
        compiled: CompilePITArtifact,
    ) -> OfficialEvaluationView:
        train = self.evidence_for(compiled.compiled_factor_hash, "TRAIN")
        validation = self.evidence_for(compiled.compiled_factor_hash, "VALIDATION")
        self.visible_calls.append(desk_id.value)
        visible = {
            "train_rank_ic": train.metrics.rank_ic,
            "validation_rank_ic": validation.metrics.rank_ic,
            "validation_icir": validation.metrics.icir,
            "validation_net_return": validation.metrics.net_return,
            "validation_observation_count": float(validation.metrics.observation_count),
        }
        return OfficialEvaluationView(
            evidence_hash=validation.artifact_hash,
            redacted_summary=RedactedEvidenceSummary(
                evidence_hash=validation.artifact_hash,
                visible_metrics=visible,
                allowed_notes=("official train/validation redacted summary only",),
            ),
        )

    def _identity(self, factor_hash: str) -> ExperimentIdentity:
        return ExperimentIdentity(
            experiment_id="integration-" + factor_hash.removeprefix("sha256:")[:20],
            dataset_hash=self.bundle.dataset_card.snapshot_hash,
            universe_hash=self.bundle.universe_manifest.artifact_hash,
            factor_hash=factor_hash,
            split_hash=self.bundle.split_manifest.artifact_hash,
            label_hash=self.bundle.label_specification.artifact_hash,
            cost_hash=self.cost_model.artifact_hash,
            code_hash=canonical_content_hash({"service": "A-F-integration", "version": "v1"}),
            availability_hash=self.snapshot.artifact_hash,
            seed=self.seed,
        )

    def _factor_result(self, factor_hash: str) -> FactorResult:
        cached = self._results.get(factor_hash)
        if cached is not None:
            return cached
        try:
            dag = self._dags[factor_hash]
        except KeyError as exc:
            raise IntegrationInvariantError("factor was not registered with the deterministic authority") from exc
        result = execute_factor_dag(dag, self.snapshot, seed=self.seed, config={"mode": "offline-integration"})
        self._results[factor_hash] = result
        return result

    def evidence_for(self, factor_hash: str, partition: str) -> OfficialEvidence:
        normalized = partition.upper()
        key = (factor_hash, normalized)
        cached = self._evidence.get(key)
        if cached is not None:
            return cached
        factor_result = self._factor_result(factor_hash)
        rows: list[EvaluationRow] = []
        for score in factor_result.values:
            label = self._label_by_key.get((score.event_time, score.symbol))
            feature = self._feature_by_key.get((score.event_time, score.symbol))
            if score.value is None or label is None or feature is None or label.partition.upper() != normalized:
                continue
            rows.append(
                EvaluationRow(
                    event_time=score.event_time,
                    symbol=score.symbol,
                    score=score.value,
                    label=label.value,
                    market_regime="frozen_fixture",
                    volume=feature.values["volume"],
                )
            )
        evidence = self.service.evaluate(
            identity=self._identity(factor_hash),
            rows=tuple(rows),
            partition=normalized,
            cost_model=self.cost_model,
        )
        self._evidence[key] = evidence
        return evidence

    def latest_visible_scores(self, factor_hash: str) -> dict[str, float]:
        latest: dict[str, tuple[datetime, float]] = {}
        result = self._factor_result(factor_hash)
        for score in result.values:
            label = self._label_by_key.get((score.event_time, score.symbol))
            if score.value is None or label is None or label.partition.upper() != "VALIDATION":
                continue
            existing = latest.get(score.symbol)
            if existing is None or score.event_time > existing[0]:
                latest[score.symbol] = (score.event_time, score.value)
        raw = {symbol: value for symbol, (_, value) in latest.items()}
        mean = fmean(raw.values())
        return {symbol: value - mean for symbol, value in raw.items() if not math.isclose(value, mean)}


def _visible_p_value(validation: OfficialEvidence) -> float:
    metrics = validation.metrics
    if metrics.ic_mean <= 0.0:
        return 1.0
    if metrics.ic_std <= 0.0:
        return 1e-12
    statistic = metrics.ic_mean / (metrics.ic_std / math.sqrt(max(1, metrics.period_count)))
    return max(1e-12, min(1.0, math.erfc(abs(statistic) / math.sqrt(2.0))))


def _sealed_verdict(sealed: OfficialEvidence) -> bool:
    """Mechanical boolean projection; raw sealed metrics never leave this call."""

    return (
        sealed.partition == "SEALED"
        and sealed.metrics.observation_count >= 20
        and sealed.metrics.rank_ic > 0.0
        and sealed.metrics.icir > 0.0
        and sealed.metrics.net_return > 0.0
    )


def _document_draft(
    *,
    root: Path,
    bundle: ResearchDatasetBundle,
    authority: BundleEvidenceAuthority,
) -> tuple[dict[str, Any], OfficialEvidence]:
    store = FrozenDocumentStore(root / "frozen_documents")
    memory = ResearchMemoryStore(root / "proposal_memory")
    locator = "https://fixture.veriquant.invalid/frozen-momentum-note"
    gateway = FrozenWebFixtureGateway(
        {
            locator: {
                "body": "A positive short-horizon momentum hypothesis can be drafted from frozen close observations. Deterministic PIT evidence must decide it.",
            }
        }
    )
    dag = EvidenceGroundedResearchDAG(
        retriever=Retriever({SourceKind.FROZEN_WEB_FIXTURE: gateway}, store),
        synthesizer=HypothesisSynthesizer(),
        citation_critic=CitationCritic(store),
        memory_store=memory,
    )
    brief = ResearchBrief(
        question="Can a frozen document motivate a short-horizon A-share factor draft?",
        claim="Frozen close observations motivate a positive short-horizon momentum DRAFT.",
        economic_mechanism="Slow incorporation of already available price information may create short-lived continuation.",
        expected_sign="positive",
        factor_name="cited_three_day_momentum_draft",
        factor_expression="rank_cross_section(pct_change(close, 3))",
        feature_names=("close",),
        required_terms=("close", "positive", "momentum"),
        falsification_tests=("Reject if deterministic validation net evidence is non-positive.",),
        decision_time=bundle.decision_time.isoformat(),
    )
    request = SourceRequest(
        source_kind=SourceKind.FROZEN_WEB_FIXTURE,
        locator=locator,
        retrieved_at="2024-01-20T01:00:00Z",
        available_at="2024-01-20T01:05:00Z",
        metadata={
            "title": "Frozen momentum note",
            "institution": "VeriQuant Offline Fixture",
            "published_at": "2024-01-19T08:00:00Z",
        },
    )
    result = dag.run(brief, (request,))
    _require(result.status == "DRAFT_READY", "source-backed document branch did not produce DRAFT_READY")
    context = ProposalContext(
        direction_hash=canonical_content_hash({"question": brief.question}),
        universe=tuple(bundle.universe_manifest.symbols),
        horizon="5d",
        decision_time=bundle.decision_time,
        availability_assumptions=("document available_at <= decision_time", "features obey PIT availability"),
        field_roles={field: FieldRole.FEATURE for field in bundle.feature_fields},
    )
    bridge = adapt_source_backed_draft(result.hypothesis, context=context)
    _require(bridge.status == "DRAFT_ONLY", "document branch exceeded DRAFT authority")
    _require(not bridge.direct_downstream_authority, "document branch gained direct downstream authority")
    _require(bridge.proposal.state is ResearchState.DRAFT, "document branch proposal is not DRAFT")
    authority.register_dag(bridge.proposal.factor_dag)
    evidence = authority.evidence_for(bridge.proposal.factor_dag.artifact_hash, "VALIDATION")
    return json.loads(result.hypothesis.canonical_json()), evidence


def _shadow_run(
    *,
    run_id: str,
    bundle: ResearchDatasetBundle,
    fixture: Any,
    authority: BundleEvidenceAuthority,
    factor_hash: str,
    promotion: Any,
    seed: int,
) -> tuple[Any, PerformanceAttribution, ShadowLedger]:
    scores = authority.latest_visible_scores(factor_hash)
    _require(len(scores) >= 2, "CHALLENGER has no visible cross-sectional scores")
    policy_hash = content_hash(
        {
            "promotion_policy_version": promotion.decision.policy_version,
            "robustness_policy_hash": promotion.audit.robustness_policy_hash,
            "multiple_testing_policy_hash": promotion.audit.multiple_testing_policy_hash,
        }
    )
    challenger = ChallengerVersion(
        challenger_id=promotion.decision.candidate_id,
        version=promotion.decision.candidate_version,
        state=ChallengerState.CHALLENGER,
        policy_version=promotion.decision.policy_version,
        policy_hash=policy_hash,
        promotion_decision_hash=promotion.decision.decision_hash,
        evidence_hash=promotion.audit.candidate_evidence_hash,
        factor_hash=factor_hash,
        scores_by_symbol=scores,
        baseline_factor_mean=fmean(scores.values()),
        baseline_factor_std=pstdev(scores.values()),
        baseline_ic_proxy=authority.evidence_for(factor_hash, "VALIDATION").metrics.rank_ic,
        expected_contribution=authority.evidence_for(factor_hash, "VALIDATION").metrics.net_return,
    )

    latest_rows: dict[str, Any] = {}
    for row in fixture.snapshot.rows:
        if row.symbol not in scores or row.event_time > bundle.decision_time or row.available_at > bundle.decision_time:
            continue
        current = latest_rows.get(row.symbol)
        if current is None or (row.event_time, row.available_at, row.published_at) > (
            current.event_time,
            current.available_at,
            current.published_at,
        ):
            latest_rows[row.symbol] = row
    _require(set(latest_rows) == set(scores), "frozen prices do not cover the CHALLENGER score universe")
    universe = tuple(sorted(scores))
    vintage = DataVintage(
        vintage_id="offline-vintage-" + bundle.dataset_card.snapshot_hash.removeprefix("sha256:")[:16],
        revision_id=fixture.envelope.manifest.revision,
        dataset_hash=bundle.dataset_card.snapshot_hash,
        universe=universe,
        data_available_time=max(row.available_at for row in latest_rows.values()),
        decision_time=bundle.decision_time,
        corporate_action_available_time=max(row.available_at for row in latest_rows.values()),
        coverage_by_symbol={symbol: 1.0 for symbol in universe},
        beta_by_symbol={symbol: 0.0 for symbol in universe},
        industry_by_symbol={symbol: "UNCLASSIFIED" for symbol in universe},
        adv_by_symbol={symbol: float(latest_rows[symbol].values["volume"]) for symbol in universe},
    )
    execution_time = bundle.decision_time + timedelta(minutes=30)
    valuation_time = bundle.decision_time + timedelta(hours=5)
    execution_prices = {
        symbol: MarketPrice(
            symbol=symbol,
            price=float(latest_rows[symbol].values["close"]),
            price_time=latest_rows[symbol].event_time,
            available_time=latest_rows[symbol].available_at,
            price_kind="mid",
        )
        for symbol in universe
    }
    valuation_prices = {
        symbol: MarketPrice(
            symbol=symbol,
            price=float(latest_rows[symbol].values["close"]),
            price_time=latest_rows[symbol].event_time,
            available_time=latest_rows[symbol].available_at,
            price_kind="mark",
        )
        for symbol in universe
    }
    risk_policy = ShadowRiskPolicy(
        policy_version=promotion.decision.policy_version,
        policy_hash=policy_hash,
    )
    request = ShadowRunRequest(
        run_id=run_id,
        account_id="offline-shadow-only",
        vintage_id=vintage.vintage_id,
        policy_version=risk_policy.policy_version,
        policy_hash=risk_policy.policy_hash,
        decision_time=bundle.decision_time,
        execution_time=execution_time,
        valuation_time=valuation_time,
        starting_cash=100000.0,
        previous_positions={},
        max_gross_weight=0.60,
        seed=seed,
    )
    ledger = ShadowLedger()
    service = ShadowPortfolioService(
        snapshot_provider=InMemoryFrozenSnapshotProvider({vintage.vintage_id: vintage}),
        challenger_provider=InMemoryApprovedChallengerProvider((challenger,)),
        price_provider=InMemoryMarketPriceProvider(
            execution_prices=execution_prices,
            valuation_prices=valuation_prices,
        ),
        portfolio_constructor=EqualWeightPortfolioConstructor(),
        risk_policy_reader=StaticRiskPolicyReader(risk_policy),
        ledger=ledger,
        clock=DeterministicClock(valuation_time + timedelta(minutes=5)),
    )
    manifest = service.run_once(request)
    count = ledger.event_count
    replay = service.run_once(request)
    _require(replay.artifact_hash == manifest.artifact_hash, "shadow run_once is not idempotent")
    _require(ledger.event_count == count == 5, "shadow replay duplicated append-only events")
    attribution = next(
        (item for item in ledger.snapshot().payloads if isinstance(item, PerformanceAttribution)),
        None,
    )
    _require(attribution is not None, "shadow attribution was not committed")
    return manifest, attribution, ledger


def _markdown_report(result: Mapping[str, Any]) -> str:
    return "\n".join(
        (
            "# VeriQuant A-F Offline Integration Acceptance",
            "",
            f"- Status: **{result['status']}**",
            f"- Run: `{result['run_id']}`",
            f"- Frozen snapshot: `{result['artifact_hashes']['frozen_snapshot']}`",
            f"- Selected FactorDAG: `{result['artifact_hashes']['factor_dag']}`",
            f"- Promotion: `{result['promotion']['state']}`",
            f"- Shadow manifest: `{result['artifact_hashes']['shadow_manifest']}`",
            f"- Workbench detail: `{result['artifact_hashes']['workbench_detail']}`",
            "",
            "The data source is the deterministic offline A-share fixture. It is not a real Tushare smoke test.",
            "The document branch remains DRAFT_ONLY and is absent from the promotion family.",
            "No network, API key, pyqlib, LLM provider, AgentLoop, broker, or live order path is required.",
            "",
        )
    )


def run_offline_acceptance(
    *,
    output_root: str | Path,
    seed: int = 7,
    workbench_root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute the automatic frozen-data-to-Workbench offline acceptance chain."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise IntegrationInvariantError("seed must be a non-negative integer")
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    publication_root = Path(workbench_root).resolve() if workbench_root is not None else output / "workbench_artifacts"
    final_run_id = run_id or f"v1-offline-seed-{seed}"

    fixture = build_offline_real_research_fixture()
    bridge_request = build_default_bridge_request(
        snapshot_id=fixture.snapshot_id,
        decision_time=fixture.decision_time,
        split_manifest=fixture.split_manifest,
        feature_fields=fixture.feature_fields,
    )
    bundle = AShareResearchBridge(fixture.reader).build_bundle(bridge_request)
    snapshot = _bundle_snapshot(bundle)
    authority = BundleEvidenceAuthority(bundle=bundle, snapshot=snapshot, seed=seed)

    desks = build_default_desk_configs()
    question = ResearchQuestionView(
        question_id="integration-offline-a-share",
        text="Propose bounded A-share research DRAFTs from one frozen PIT bundle.",
        universe=tuple(bundle.universe_manifest.symbols),
        horizon="5d",
        decision_time=bundle.decision_time,
        availability_assumptions=tuple(bundle.dataset_card.availability_assumptions),
    )
    controller = ResearchStageController(compiler=authority, evaluator=authority)
    desk_result = controller.run_all(
        desks=desks,
        question=question,
        providers={desk.desk_id: DeterministicProposalProvider() for desk in desks},
        tools={desk.desk_id: build_default_tools_for_desk(desk.desk_id) for desk in desks},
    )
    completed = [item for item in desk_result.results if item.status is LLMDeskStatus.DRAFT]
    incomplete = [item for item in desk_result.results if item.status is not LLMDeskStatus.DRAFT]
    _require(completed, "all six Desk branches failed")
    _require(incomplete, "fixture did not demonstrate branch failure isolation")
    selected = next((item for item in completed if item.desk_id is DeskId.EXPECTATIONS_EVENTS), completed[0])
    _require(selected.factor_draft is not None and selected.compile_pit is not None, "selected Desk is incomplete")
    factor_hash = selected.compile_pit.compiled_factor_hash

    train = authority.evidence_for(factor_hash, "TRAIN")
    validation = authority.evidence_for(factor_hash, "VALIDATION")
    family = build_family_manifest(
        family_id="integration-visible-family",
        manifest_version="v1",
        frozen_at=bundle.decision_time,
        candidates=(("candidate-visible-momentum", "v1"),),
    )
    view = candidate_view_from_official_evidence(
        candidate_id="candidate-visible-momentum",
        candidate_version="v1",
        family_manifest=family,
        train=train,
        validation=validation,
        proposer_id=f"desk-{selected.desk_id.value}",
        p_value=_visible_p_value(validation),
        coverage=min(bundle.data_quality_manifest.coverage_by_field.values()),
    )
    sealed = authority.evidence_for(factor_hash, "SEALED")
    reader = InMemoryOfficialEvidenceReader(
        manifest=family,
        views=(view,),
        sealed_passed=_sealed_verdict(sealed),
    )
    promotion = evaluate_statistical_promotion(
        reader=reader,
        request=PromotionEvaluationRequest(
            candidate_id=view.candidate_id,
            candidate_version=view.candidate_version,
            family_id=family.family_id,
            manifest_version=family.manifest_version,
            idempotency_key=f"{final_run_id}-promotion",
            proposer_id=view.proposer_id,
            approver_id="integration-operator",
            decision_time=bundle.decision_time,
        ),
    )
    _require(promotion.decision.to_state is PromotionState.CHALLENGER, "statistical gate did not produce CHALLENGER")

    document_hypothesis, document_evidence = _document_draft(
        root=output / "document_branch",
        bundle=bundle,
        authority=authority,
    )
    _require(document_evidence.partition == "VALIDATION", "document DRAFT did not reach deterministic visible Evidence")

    shadow, attribution, ledger = _shadow_run(
        run_id=final_run_id,
        bundle=bundle,
        fixture=fixture,
        authority=authority,
        factor_hash=factor_hash,
        promotion=promotion,
        seed=seed,
    )

    promotion_audit = promotion.audit.to_dict()
    promotion_audit.pop("sealed_handle_hash", None)
    artifacts = {
        "overview": {
            "run_id": final_run_id,
            "status": "SHADOW_ACTIVE",
            "decision_time": bundle.decision_time.isoformat(),
            "data_source": bundle.dataset_card.source,
            "research_dag": {
                item.desk_id.value: item.status.value for item in desk_result.results
            },
            "authority": "deterministic FactorDAG/PIT/Evidence/Promotion/Shadow services",
        },
        "lineage": {
            "selected_desk": selected.desk_id.value,
            "factor_dag_hash": factor_hash,
            "completed_desks": [item.desk_id.value for item in completed],
            "failed_or_incomplete_desks": [item.desk_id.value for item in incomplete],
        },
        "evidence": {
            "evidence_class": "OFFICIAL_EVIDENCE",
            "train": train.to_dict(),
            "validation": validation.to_dict(),
            "sealed_status": "EVALUATED_BY_PROMOTION_CONTROLLER",
            "sealed_redacted": True,
        },
        "critics": {
            "statistical_robustness": promotion.robustness.to_dict() if promotion.robustness else {},
            "branch_failures": {
                item.desk_id.value: item.failure_code for item in incomplete
            },
        },
        "promotion": {
            "decision": promotion.decision.to_dict(),
            "audit": promotion_audit,
        },
        "citations": {
            "status": "DRAFT_ONLY",
            "direct_downstream_authority": False,
            "hypothesis": document_hypothesis,
            "validation_evidence_hash": document_evidence.artifact_hash,
        },
        "shadow_portfolio": shadow.to_dict(),
        "nav_attribution": attribution.to_dict(),
        "drift_alerts": {"items": [item.to_dict() for item in shadow.alerts]},
    }
    publication = publish_workbench_run(
        root=publication_root,
        run_id=final_run_id,
        status="SHADOW_ACTIVE",
        updated_at=bundle.decision_time.isoformat(),
        artifacts=artifacts,
    )

    result: dict[str, Any] = {
        "schema_version": "veriquant-integration-e2e/v1",
        "status": "PASSED",
        "run_id": final_run_id,
        "mode": "OFFLINE_DETERMINISTIC",
        "seed": seed,
        "capability_availability": probe_optional_capabilities(),
        "research_desks": {
            "completed": [item.desk_id.value for item in completed],
            "failed_or_incomplete": [item.desk_id.value for item in incomplete],
            "shared_official_authority": "single-bundle-evidence-service",
            "official_visible_calls": list(authority.visible_calls),
        },
        "document_branch": {
            "status": "DRAFT_ONLY",
            "validation_evidence_hash": document_evidence.artifact_hash,
            "included_in_promotion_family": False,
        },
        "promotion": {
            "state": promotion.decision.to_state.value,
            "verdict": promotion.decision.verdict.value,
            "decision_hash": promotion.decision.decision_hash,
        },
        "shadow": {
            "status": shadow.status.value,
            "ledger_events": ledger.event_count,
            "idempotent_replay": True,
            "nav_end": attribution.nav_end,
            "turnover": attribution.turnover,
            "transaction_cost": attribution.transaction_cost,
        },
        "workbench": publication,
        "artifact_hashes": {
            "frozen_snapshot": bundle.dataset_card.snapshot_hash,
            "research_bundle": bundle.artifact_hash,
            "pit_snapshot": snapshot.artifact_hash,
            "factor_dag": factor_hash,
            "train_evidence": train.artifact_hash,
            "validation_evidence": validation.artifact_hash,
            "promotion_decision": promotion.decision.decision_hash,
            "shadow_manifest": shadow.artifact_hash,
            "shadow_ledger_tip": ledger.tip_hash,
            "workbench_detail": publication["detail_hash"],
        },
        "v1_gate_evidence": {
            "offline_end_to_end": "IMPLEMENTED_AND_TESTED",
            "real_tushare_smoke": "UNAVAILABLE",
            "twenty_day_shadow_replay": "UNAVAILABLE",
            "security_authority": "PARTIALLY_TESTED_SEPARATELY",
            "recoverability_reproducibility": "PARTIALLY_TESTED",
        },
    }
    deterministic_projection = {key: value for key, value in result.items() if key != "artifact_hash"}
    result["artifact_hash"] = canonical_content_hash(deterministic_projection)
    _atomic_write_text(output / "result.json", json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    _atomic_write_text(output / "report.md", _markdown_report(result))
    return result


__all__ = ["BundleEvidenceAuthority", "IntegrationInvariantError", "run_offline_acceptance"]
