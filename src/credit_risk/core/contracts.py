from __future__ import annotations

from pathlib import Path

REQUIRED_HOME_CREDIT_FILES = {
    "application_train.csv",
    "application_test.csv",
    "bureau.csv",
    "previous_application.csv",
    "POS_CASH_balance.csv",
    "credit_card_balance.csv",
    "installments_payments.csv",
}


def validate_data_dir(data_dir: Path) -> list[str]:
    return sorted(name for name in REQUIRED_HOME_CREDIT_FILES if not (data_dir / name).is_file())
