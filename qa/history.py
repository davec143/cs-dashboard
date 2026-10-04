"""Optional private historical audit; no customer/employee records ship with code."""
import json
import os
from pathlib import Path


def history():
    root = Path(os.environ.get('QA_AUDIT_DIR', Path(__file__).resolve().parents[1] / 'audit'))
    facts, manifest = root / 'live-findings.json', root / 'legacy-manifest.json'
    if facts.exists() and manifest.exists():
        return json.loads(facts.read_text()), json.loads(manifest.read_text())
    return {'rows':0,'uniqueCalls':0,'duplicateRows':0,'duplicatedCalls':[], 'monthly':{},'agents':{},
            'types':{'Evaluated':0},'totalDisagreement':[],'totalDisagreementCount':0,
            'junkRows':[],'evaluatedMissingTotal':[],'latestCallDate':None}, []
