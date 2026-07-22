export type ResearchModeId =
  | "NEW_ALPHA_EXPLORATION"
  | "VALIDATE_IDEA"
  | "IMPROVE_EXISTING_FACTOR"
  | "PORTFOLIO_GAP_SEARCH"
  | "DIAGNOSE_FACTOR_PORTFOLIO"
  | "EVENT_MARKET_TOPIC"
  | "PAPER_STRATEGY_REPLICATION"
  | "NEW_DATA_SOURCE_EVALUATION"
  | "PRELAUNCH_REVIEW"
  | "MONITOR_DECAY_RETIREMENT";

export type ResearchDepthId =
  | "QUICK_EXPLORATION"
  | "STANDARD_RESEARCH"
  | "STRICT_VALIDATION";

export type InteractionStyleId = "GUIDED" | "STANDARD" | "BATCH";

export type CampaignLifecycleState =
  | "DRAFT"
  | "NEEDS_CONFIRMATION"
  | "CONFIRMED"
  | "ROUTED"
  | "RUNNING"
  | "AWAITING_CHECKPOINT"
  | "VALIDATING"
  | "COMPLETED"
  | "STOPPED"
  | "BLOCKED"
  | "FAILED";

export type RuntimeMode = "DETERMINISTIC" | "REAL_LLM" | "BLOCKED";
export type ProviderAvailability = "AVAILABLE" | "UNAVAILABLE";
export type DeskStatus = "WAITING" | "RUNNING" | "COMPLETED" | "FAILED" | "BLOCKED";
export type StageStatus = "WAITING" | "RUNNING" | "COMPLETED" | "FAILED" | "BLOCKED";
export type FactorLibraryActionId = "validate" | "improve" | "diagnose" | "gap";

export interface ResearchModeOption {
  id: ResearchModeId;
  title: string;
  exampleQuestion: string;
  systemWork: string;
  output: string;
  methods: string[];
  desks: string[];
  advanced?: boolean;
}

export interface ResearchDepthOption {
  id: ResearchDepthId;
  title: string;
  description: string;
  boundary: string;
}

export interface InteractionStyleOption {
  id: InteractionStyleId;
  title: string;
  description: string;
}

export interface CampaignBudgetView {
  factorFamilies: number;
  trials: number;
  modelCalls: number;
  runtimeMinutes: number;
}

export interface CampaignDraftView {
  schemaVersion: "alphaforge.campaign-ui/v1";
  draftId: string;
  campaignId?: string;
  state: "NEEDS_CONFIRMATION" | "CONFIRMED";
  question: string;
  mode: ResearchModeId;
  depth: ResearchDepthId;
  interactionStyle: InteractionStyleId;
  researchObject: string;
  universe: string[];
  horizon: string;
  allowedData: string[];
  methodModels: string[];
  selectedDesks: string[];
  budget: CampaignBudgetView;
  checkpoints: string[];
  stopConditions: string[];
  runtimeMode: RuntimeMode;
  allowsOfficialEvidence: boolean;
  allowsPromotion: false;
  fixtureFingerprint: string;
}

export interface CampaignDraftInput {
  question: string;
  mode: ResearchModeId;
  depth: ResearchDepthId;
  interactionStyle: InteractionStyleId;
}

export interface CampaignRecommendation {
  recommendedMode: ResearchModeId;
  explanation: string;
  questions: string[];
  canRun: false;
}

export interface CampaignStageView {
  id: string;
  title: string;
  status: StageStatus;
  detail: string;
}

export interface CampaignDeskView {
  id: string;
  title: string;
  status: DeskStatus;
  runtimeMode: RuntimeMode;
  budgetUsed: number;
  budgetLimit: number;
  stages: CampaignStageView[];
  failure?: string;
}

export interface CampaignResearchOutputView {
  task_id: string;
  desk_id: string;
  status: "DRAFT" | "INCOMPLETE" | "BLOCKED";
  failure_code?: string | null;
  message?: string | null;
  authority: "DRAFT_ONLY";
  hypothesis?: {
    statement: string;
    economic_mechanism: string;
    expected_sign: string;
    falsification_tests: string[];
    artifact_hash: string;
  } | null;
  factor_draft?: {
    name: string;
    description: string;
    expression: string;
    variables: string[];
    lookback_bars: number;
    neutralization: string[];
    artifact_hash: string;
  } | null;
  compile_smoke?: {
    status: string;
    compiled_factor_hash: string;
    pit_snapshot_hash: string;
    is_production_pit: false;
  } | null;
  redacted_feedback?: {
    explanation: string;
    artifact_hash: string;
  } | null;
}

export interface CampaignRunView {
  campaignId: string;
  state: CampaignLifecycleState;
  runtimeMode: RuntimeMode;
  selectedDesks: CampaignDeskView[];
  researchOutputs?: CampaignResearchOutputView[];
  budget: CampaignBudgetView;
  trialsUsed: number;
  currentCheckpoint: string;
  checkpointState: "PENDING" | "REACHED" | "CONFIRMED";
  messages: string[];
  reportPath?: string;
  workbenchPath?: string;
  artifactHash?: string;
  factorDagHash?: string;
  workbenchDetailHash?: string;
  shadowStatus?: string;
}

/** Credential-free provider readiness for the Campaign workspace. */
export interface CampaignProviderStatusView {
  status: ProviderAvailability;
  host?: string;
  model?: string;
  reasonCode?: string;
}

export interface FactorLibraryItemView {
  factorId: string;
  displayName: string;
  theme: string;
  eligibility:
    | "DISCOVERED"
    | "METADATA_VALIDATED"
    | "DAG_PORTED"
    | "PIT_TESTED"
    | "AUXILIARY_ONLY"
    | "REJECTED";
  qualificationStatus:
    | "DIRECT_FACTOR_DAG"
    | "NEEDS_DETERMINISTIC_OPERATOR"
    | "UNSAFE_AUXILIARY_ONLY"
    | "REJECTED";
  sourceFamily: "academic" | "alpha101" | "fundamental" | "gtja191" | "qlib158";
  compatibilityClass: "directly_representable" | "needs_deterministic_operator" | "auxiliary_only" | "rejected";
  lineage: string;
  visibleSummary: string;
  actions: FactorLibraryActionId[];
}

export interface FactorLibraryCatalogView {
  schemaVersion: "alphaforge.factor-library-ui/v1";
  total: 461;
  generatedFrom: "static_factor_library_qualification";
  catalogHash: string;
  qualificationHash: string;
  items: FactorLibraryItemView[];
}

export interface CampaignApiAdapter {
  createDraft(input: CampaignDraftInput): Promise<CampaignDraftView>;
  recommend(question: string): Promise<CampaignRecommendation>;
  confirm(draft: CampaignDraftView): Promise<CampaignDraftView>;
  getRun(campaignId: string): Promise<CampaignRunView>;
  getProviderStatus(): Promise<CampaignProviderStatusView>;
  startRun(campaignId: string, runtimeMode?: "DETERMINISTIC" | "REAL_LLM"): Promise<CampaignRunView>;
  listFactors(): Promise<FactorLibraryItemView[]>;
}
