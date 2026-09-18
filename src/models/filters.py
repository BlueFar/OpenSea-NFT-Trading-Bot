from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional

class DataQualityState(str, Enum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    MISSING = "MISSING"
    ERROR = "ERROR"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

class FilterResultStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    OBSERVE = "OBSERVE"  # For advisory/observe-only metrics like unconfirmed offer/floor ratio

@dataclass
class FilterCriterionResult:
    name: str
    threshold: str
    actual_value: Any
    unit: str
    formula: str
    result: FilterResultStatus
    data_quality: DataQualityState
    timestamp: str
    source: str
    notes: Optional[str] = None

@dataclass
class FilterEvaluationReport:
    collection_slug: str
    is_overall_pass: bool
    criteria: Dict[str, FilterCriterionResult] = field(default_factory=dict)
    rejection_reasons: List[str] = field(default_factory=list)

    def add_criterion(self, criterion: FilterCriterionResult):
        self.criteria[criterion.name] = criterion
        if criterion.result in (FilterResultStatus.FAIL, FilterResultStatus.DATA_INSUFFICIENT):
            self.rejection_reasons.append(
                f"{criterion.name}: actual={criterion.actual_value}{criterion.unit} (threshold={criterion.threshold}) - {criterion.result.value}"
            )
