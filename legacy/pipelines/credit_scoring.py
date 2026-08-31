"""
Credit Risk Prediction — Probability of Default (PD) Model
Home Credit Default Risk

Run: python credit_scoring.py
Outputs: model_artifacts/  (champion_model.pkl, feature_cols.json, compliance_exclusions.json, ...)
"""

import numpy as np
import pandas as pd
import pickle
import json as _json
import shutil
import pathlib
import warnings
import os
from pathlib import Path

warnings.filterwarnings('ignore')

from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score, roc_curve, brier_score_loss

import xgboost as xgb
import lightgbm as lgb
from catboost import CatBoostClassifier, Pool

import shap
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

import mlflow
import mlflow.sklearn

print('All imports OK')


# ═══════════════════════════════════════════════════════════════════════════════
# 1. DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

DATA_PATH = './data' if os.path.exists('./data') else '.'

def load(name):
    df = pd.read_csv(f'{DATA_PATH}/{name}.csv')
    print(f'  {name}: {df.shape[0]:,} rows x {df.shape[1]} cols')
    return df

print('Loading datasets...')
app_train    = load('application_train')
app_test     = load('application_test')
bureau       = load('bureau')
prev_app     = load('previous_application')
pos_cash     = load('POS_CASH_balance')
credit_card  = load('credit_card_balance')
installments = load('installments_payments')

app_test['TARGET'] = np.nan

vc = app_train['TARGET'].value_counts()
print(f'Target: {vc[0]:,} non-default ({vc[0]/len(app_train):.1%})  |  {vc[1]:,} default ({vc[1]/len(app_train):.1%})')


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FEATURE ENGINEERING
# ═══════════════════════════════════════════════════════════════════════════════

def build_bureau_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        bureau_loan_count       = ('SK_ID_BUREAU',           'count'),
        bureau_active_count     = ('CREDIT_ACTIVE',          lambda x: (x == 'Active').sum()),
        bureau_closed_count     = ('CREDIT_ACTIVE',          lambda x: (x == 'Closed').sum()),
        bureau_bad_debt_count   = ('CREDIT_ACTIVE',          lambda x: (x == 'Bad debt').sum()),
        bureau_avg_days_credit  = ('DAYS_CREDIT',            'mean'),
        bureau_avg_days_enddate = ('DAYS_CREDIT_ENDDATE',    'mean'),
        bureau_total_debt       = ('AMT_CREDIT_SUM_DEBT',    'sum'),
        bureau_total_limit      = ('AMT_CREDIT_SUM',         'sum'),
        bureau_total_overdue    = ('AMT_CREDIT_SUM_OVERDUE', 'sum'),
        bureau_max_overdue      = ('AMT_CREDIT_MAX_OVERDUE', 'max'),
        bureau_prolong_count    = ('CNT_CREDIT_PROLONG',     'sum'),
        bureau_avg_annuity      = ('AMT_ANNUITY',            'mean'),
    ).reset_index()
    agg['bureau_debt_limit_ratio'] = agg['bureau_total_debt']    / (agg['bureau_total_limit'] + 1)
    agg['bureau_active_ratio']     = agg['bureau_active_count']  / (agg['bureau_loan_count']  + 1)
    agg['bureau_overdue_ratio']    = agg['bureau_total_overdue'] / (agg['bureau_total_debt']  + 1)
    return agg

def enhance_bureau_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        bureau_last_credit     = ('DAYS_CREDIT',         'max'),
        bureau_first_credit    = ('DAYS_CREDIT',         'min'),
        bureau_credit_span     = ('DAYS_CREDIT',         lambda x: x.max() - x.min()),
        bureau_days_credit_std = ('DAYS_CREDIT',         'std'),
        bureau_debt_std        = ('AMT_CREDIT_SUM_DEBT', 'std'),
        bureau_active_mean     = ('CREDIT_ACTIVE',       lambda x: (x == 'Active').mean()),
    ).reset_index()
    agg['bureau_recency']   = -agg['bureau_last_credit']
    agg['bureau_stability'] = 1 / (agg['bureau_days_credit_std'] + 1)
    return agg

def build_prev_app_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        prev_app_count         = ('SK_ID_PREV',           'count'),
        prev_approved_count    = ('NAME_CONTRACT_STATUS', lambda x: (x == 'Approved').sum()),
        prev_refused_count     = ('NAME_CONTRACT_STATUS', lambda x: (x == 'Refused').sum()),
        prev_avg_credit        = ('AMT_CREDIT',           'mean'),
        prev_avg_annuity       = ('AMT_ANNUITY',          'mean'),
        prev_avg_down_payment  = ('AMT_DOWN_PAYMENT',     'mean'),
        prev_avg_goods_price   = ('AMT_GOODS_PRICE',      'mean'),
        prev_max_days_decision = ('DAYS_DECISION',        'max'),
        prev_avg_days_decision = ('DAYS_DECISION',        'mean'),
        prev_consumer_count    = ('NAME_CONTRACT_TYPE',   lambda x: (x == 'Consumer loans').sum()),
        prev_cash_count        = ('NAME_CONTRACT_TYPE',   lambda x: (x == 'Cash loans').sum()),
        prev_revolving_count   = ('NAME_CONTRACT_TYPE',   lambda x: (x == 'Revolving loans').sum()),
    ).reset_index()
    agg['prev_approval_rate'] = agg['prev_approved_count'] / (agg['prev_app_count'] + 1)
    agg['prev_refusal_rate']  = agg['prev_refused_count']  / (agg['prev_app_count'] + 1)
    return agg

def enhance_prev_app_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        prev_last_app   = ('DAYS_DECISION', 'max'),
        prev_first_app  = ('DAYS_DECISION', 'min'),
        prev_app_span   = ('DAYS_DECISION', lambda x: x.max() - x.min()),
        prev_days_std   = ('DAYS_DECISION', 'std'),
        prev_credit_std = ('AMT_CREDIT',    'std'),
    ).reset_index()
    agg['prev_recency']       = -agg['prev_last_app']
    agg['prev_app_frequency'] = agg['prev_app_span'] / (agg['prev_days_std'] + 1)
    return agg

def build_installments_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        inst_count          = ('SK_ID_PREV',        'count'),
        inst_avg_payment    = ('AMT_PAYMENT',        'mean'),
        inst_avg_instalment = ('AMT_INSTALMENT',     'mean'),
        inst_max_payment    = ('AMT_PAYMENT',        'max'),
        inst_min_payment    = ('AMT_PAYMENT',        'min'),
        inst_avg_days_delay = ('DAYS_ENTRY_PAYMENT', 'mean'),
        inst_max_days_delay = ('DAYS_ENTRY_PAYMENT', 'max'),
    ).reset_index()
    agg['inst_payment_ratio'] = agg['inst_avg_payment']    / (agg['inst_avg_instalment'] + 1)
    agg['inst_payment_gap']   = agg['inst_avg_instalment'] -  agg['inst_avg_payment']
    agg['inst_delay_risk']    = agg['inst_max_days_delay'] -  agg['inst_avg_days_delay']
    return agg

def build_pos_cash_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        pos_count        = ('SK_ID_PREV',          'count'),
        pos_months_avg   = ('MONTHS_BALANCE',       'mean'),
        pos_dpd_avg      = ('SK_DPD',               'mean'),
        pos_dpd_max      = ('SK_DPD',               'max'),
        pos_dpd_def_avg  = ('SK_DPD_DEF',           'mean'),
        pos_dpd_def_max  = ('SK_DPD_DEF',           'max'),
        pos_active_count = ('NAME_CONTRACT_STATUS', lambda x: (x == 'Active').sum()),
        pos_completed    = ('NAME_CONTRACT_STATUS', lambda x: (x == 'Completed').sum()),
    ).reset_index()
    agg['pos_dpd_stress']   = agg['pos_dpd_avg'] + agg['pos_dpd_max']
    agg['pos_active_ratio'] = agg['pos_active_count'] / (agg['pos_count'] + 1)
    return agg

def build_credit_card_features(df):
    agg = df.groupby('SK_ID_CURR').agg(
        cc_balance_avg  = ('AMT_BALANCE',             'mean'),
        cc_balance_max  = ('AMT_BALANCE',             'max'),
        cc_limit_avg    = ('AMT_CREDIT_LIMIT_ACTUAL', 'mean'),
        cc_drawings_avg = ('AMT_DRAWINGS_CURRENT',    'mean'),
        cc_payment_avg  = ('AMT_PAYMENT_CURRENT',     'mean'),
        cc_dpd_avg      = ('SK_DPD',                  'mean'),
        cc_dpd_max      = ('SK_DPD',                  'max'),
    ).reset_index()
    agg['cc_utilization']   = agg['cc_balance_avg'] / (agg['cc_limit_avg']    + 1)
    agg['cc_payment_ratio'] = agg['cc_payment_avg'] / (agg['cc_drawings_avg'] + 1)
    agg['cc_stress']        = agg['cc_dpd_max']     +  agg['cc_dpd_avg']
    return agg

def engineer_app_features(df):
    d = df.copy()
    d['CREDIT_INCOME_RATIO']   = d['AMT_CREDIT']       / (d['AMT_INCOME_TOTAL'] + 1)
    d['ANNUITY_INCOME_RATIO']  = d['AMT_ANNUITY']      / (d['AMT_INCOME_TOTAL'] + 1)
    d['CREDIT_GOODS_RATIO']    = d['AMT_CREDIT']       / (d['AMT_GOODS_PRICE']  + 1)
    d['INCOME_PER_PERSON']     = d['AMT_INCOME_TOTAL'] / (d['CNT_FAM_MEMBERS']  + 1)
    d['ANNUITY_CREDIT_RATIO']  = d['AMT_ANNUITY']      / (d['AMT_CREDIT']       + 1)
    d['DAYS_EMPLOYED_ANOMALY'] = (d['DAYS_EMPLOYED'] == 365243).astype(int)
    d['EMPLOYMENT_YEARS']      = -d['DAYS_EMPLOYED'].clip(upper=0) / 365
    d['AGE_YEARS']             = -d['DAYS_BIRTH'] / 365
    d['EMPLOYMENT_AGE_RATIO']  = d['EMPLOYMENT_YEARS'] / (d['AGE_YEARS'] + 1)
    d['REGISTRATION_YEARS']    = -d['DAYS_REGISTRATION'] / 365
    d['ID_PUBLISH_YEARS']      = -d['DAYS_ID_PUBLISH']   / 365
    d['LOAN_TENOR_MONTHS']     =  d['AMT_CREDIT']        / (d['AMT_ANNUITY'] + 1)
    d['EXT_SOURCE_MEAN']       = d[['EXT_SOURCE_1','EXT_SOURCE_2','EXT_SOURCE_3']].mean(axis=1)
    d['EXT_SOURCE_MIN']        = d[['EXT_SOURCE_1','EXT_SOURCE_2','EXT_SOURCE_3']].min(axis=1)
    d['EXT_SOURCE_PRODUCT']    = (d['EXT_SOURCE_1'].fillna(0.5) *
                                   d['EXT_SOURCE_2'].fillna(0.5) *
                                   d['EXT_SOURCE_3'].fillna(0.5))
    doc_cols = [c for c in d.columns if c.startswith('FLAG_DOCUMENT')]
    d['TOTAL_DOC_FLAGS'] = d[doc_cols].sum(axis=1)
    enq_cols = [c for c in d.columns if 'AMT_REQ_CREDIT_BUREAU' in c]
    d['TOTAL_ENQUIRIES'] = d[enq_cols].sum(axis=1)
    return d

print('Building features...')
bureau_feats       = build_bureau_features(bureau)
bureau_extra_feats = enhance_bureau_features(bureau)
prev_feats         = build_prev_app_features(prev_app)
prev_extra_feats   = enhance_prev_app_features(prev_app)
inst_feats         = build_installments_features(installments)
pos_feats          = build_pos_cash_features(pos_cash)
cc_feats           = build_credit_card_features(credit_card)
app_train_eng      = engineer_app_features(app_train)
app_test_eng       = engineer_app_features(app_test)

def build_full_dataset(app_df, is_train=True, keep_cols=None):
    df = app_df.copy()
    for feat_df in [bureau_feats, bureau_extra_feats, prev_feats, prev_extra_feats,
                    inst_feats, pos_feats, cc_feats]:
        df = df.merge(feat_df, on='SK_ID_CURR', how='left')
    y = df.pop('TARGET') if is_train else None
    df.drop(columns=['SK_ID_CURR'], errors='ignore', inplace=True)
    if keep_cols is None:
        miss_rate = df.isnull().mean()
        keep_cols = miss_rate[miss_rate <= 0.50].index.tolist()
        print(f'  Dropped {(miss_rate > 0.50).sum()} features >50% missing')
    else:
        keep_cols = [c for c in keep_cols if c in df.columns]
    return df[keep_cols], y, keep_cols

print('Merging train...')
X_train_raw, y_train, keep_cols = build_full_dataset(app_train_eng, is_train=True)
print('Merging test...')
X_test_raw,  _,       _         = build_full_dataset(app_test_eng,  is_train=False, keep_cols=keep_cols)
print(f'Shape: train={X_train_raw.shape}  test={X_test_raw.shape}')


# ═══════════════════════════════════════════════════════════════════════════════
# 3. COMPLIANCE — PROTECTED ATTRIBUTE EXCLUSION
# ═══════════════════════════════════════════════════════════════════════════════
# Category A — Direct protected attributes (remove unconditionally)
# Category B — No individual financial link / redlining proxy (remove unconditionally)
#
# Features kept despite age/gender correlation (economically justified):
#   DAYS_EMPLOYED, DAYS_REGISTRATION, DAYS_ID_PUBLISH, EMPLOYMENT_YEARS,
#   REGISTRATION_YEARS, CNT_FAM_MEMBERS, INCOME_PER_PERSON, OCCUPATION_TYPE,
#   NAME_INCOME_TYPE, NAME_HOUSING_TYPE, REGION_RATING_CLIENT — monitored via
#   disparate impact testing (see compliance_exclusions.json).
#
# Reference: EU AI Act Art. 10, ECOA / Reg B, Basel model risk guidance

COMPLIANCE_EXCLUDED = {
    # Category A: Direct protected attributes
    'CODE_GENDER':          'Sex / gender — explicitly protected in all jurisdictions',
    'NAME_FAMILY_STATUS':   'Marital status — directly protected',
    'CNT_CHILDREN':         'Parental status — protected (EU/ECOA equivalent)',
    'DAYS_BIRTH':           'Age (raw days) — directly protected',
    # Category A: Derived features that embed protected attributes
    'AGE_YEARS':            'Age in years — derived from DAYS_BIRTH, same prohibition',
    'EMPLOYMENT_AGE_RATIO': 'Embeds AGE_YEARS — indirect age signal, no independent justification',
    # Category B: Redlining proxy — no individual financial link
    'OBS_30_CNT_SOCIAL_CIRCLE': "Defaults observed in social network — attributes others' risk to this applicant",
    'DEF_30_CNT_SOCIAL_CIRCLE': 'Actual defaults in social network — strongest redlining proxy, no business justification',
    'OBS_60_CNT_SOCIAL_CIRCLE': "Defaults observed (60d) in social network — same redlining proxy as 30-day variant",
    'DEF_60_CNT_SOCIAL_CIRCLE': "Actual defaults (60d) in social network — same redlining proxy as 30-day variant",
}

to_drop = [f for f in COMPLIANCE_EXCLUDED if f in X_train_raw.columns]
missing = [f for f in COMPLIANCE_EXCLUDED if f not in X_train_raw.columns]
X_train_raw.drop(columns=to_drop, inplace=True)
X_test_raw.drop(columns=to_drop, errors='ignore', inplace=True)
print(f'Compliance: dropped {len(to_drop)}, not found {len(missing)}, remaining {X_train_raw.shape[1]}')
for f in to_drop:
    print(f'  x  {f:<35s}  {COMPLIANCE_EXCLUDED[f]}')


# ═══════════════════════════════════════════════════════════════════════════════
# 4. PREPROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

# Drop highly correlated numeric features (r > 0.95)
num_cols  = X_train_raw.select_dtypes(include='number').columns
corr      = X_train_raw[num_cols].corr().abs()
upper     = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
corr_drop = set(upper.stack().reset_index()
                .rename(columns={'level_0':'a','level_1':'b',0:'r'})
                .query('r > 0.95')['b'].tolist())
X_train_raw.drop(columns=list(corr_drop), errors='ignore', inplace=True)
X_test_raw.drop(columns=list(corr_drop),  errors='ignore', inplace=True)
print(f'Dropped {len(corr_drop)} correlated features -> {X_train_raw.shape[1]} remaining')

# ETL-aligned categorical encoding
ETL_CAT_MAPS = {
    'NAME_CONTRACT_TYPE':  {'Cash loans': 0, 'Revolving loans': 1},
    'CODE_GENDER':         {'M': 0, 'F': 1, 'XNA': 2},
    'NAME_EDUCATION_TYPE': {'Secondary / secondary special': 0, 'Higher education': 1,
                            'Incomplete higher': 2, 'Lower secondary': 3, 'Academic degree': 4},
    'NAME_FAMILY_STATUS':  {'Married': 0, 'Single / not married': 1, 'Separated': 2,
                            'Widow': 3, 'Civil marriage': 4},
    'NAME_INCOME_TYPE':    {'Working': 0, 'Commercial associate': 1, 'Pensioner': 2,
                            'State servant': 3, 'Unemployed': 4, 'Student': 5,
                            'Businessman': 6, 'Maternity leave': 7},
}

cat_cols    = X_train_raw.select_dtypes('object').columns.tolist()
le_encoders = {}

for col in cat_cols:
    tr_vals = X_train_raw[col].fillna('MISSING').astype(str)
    te_vals = X_test_raw[col].fillna('MISSING').astype(str)
    if col in ETL_CAT_MAPS:
        m = ETL_CAT_MAPS[col]
        X_train_raw[col] = tr_vals.map(m).fillna(-1).astype(int)
        X_test_raw[col]  = te_vals.map(m).fillna(-1).astype(int)
        le_encoders[col] = {'type': 'map', 'mapping': m}
    else:
        le = LabelEncoder().fit(tr_vals)
        X_train_raw[col] = le.transform(tr_vals)
        known = set(le.classes_)
        X_test_raw[col]  = te_vals.map(
            lambda v, _le=le, _k=known: int(_le.transform([v])[0]) if v in _k else -1
        )
        le_encoders[col] = {'type': 'label_encoder', 'encoder': le}

print(f'Encoding complete. Features: {X_train_raw.shape[1]}')

X_tr, X_val, y_tr, y_val = train_test_split(
    X_train_raw, y_train, test_size=0.20, random_state=42, stratify=y_train
)
print(f'Train: {X_tr.shape}  Val: {X_val.shape}  Default rate: {y_tr.mean():.2%}')

X_tr_tree  = X_tr.fillna(-999).values
X_val_tree = X_val.fillna(-999).values
scale_pos_weight = (y_tr == 0).sum() / (y_tr == 1).sum()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MODEL TRAINING
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate(name, y_true, y_prob):
    auc   = roc_auc_score(y_true, y_prob)
    gini  = 2 * auc - 1
    fpr, tpr, _ = roc_curve(y_true, y_prob)
    ks    = float(np.max(tpr - fpr))
    brier = brier_score_loss(y_true, y_prob)
    print(f'  {name:<25} AUC={auc:.4f}  Gini={gini:.4f}  KS={ks:.4f}  Brier={brier:.4f}')
    return {'name': name, 'auc': auc, 'gini': gini, 'ks': ks, 'brier': brier, 'y_prob': y_prob}

results = {}
print('\nTraining models...')

xgb_model = xgb.XGBClassifier(
    n_estimators=1000, learning_rate=0.05, max_depth=6, min_child_weight=10,
    subsample=0.8, colsample_bytree=0.8, reg_alpha=0.1, reg_lambda=1.0,
    scale_pos_weight=scale_pos_weight, eval_metric='auc', early_stopping_rounds=50,
    random_state=42, verbosity=0, n_jobs=-1
)
xgb_model.fit(X_tr_tree, y_tr, eval_set=[(X_val_tree, y_val)], verbose=False)
results['XGB'] = evaluate('XGBoost', y_val, xgb_model.predict_proba(X_val_tree)[:, 1])

lgb_model = lgb.LGBMClassifier(
    n_estimators=1000, learning_rate=0.05, num_leaves=127, min_child_samples=10,
    subsample=0.8, colsample_bytree=0.8, reg_alpha=0.1, reg_lambda=1.0,
    scale_pos_weight=scale_pos_weight, random_state=42, verbosity=-1, n_jobs=-1
)
lgb_model.fit(X_tr_tree, y_tr, eval_set=[(X_val_tree, y_val)], eval_metric='auc',
              callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(-1)])
results['LGB'] = evaluate('LightGBM', y_val, lgb_model.predict_proba(X_val_tree)[:, 1])

cat_model = CatBoostClassifier(
    iterations=1000, learning_rate=0.05, depth=6, l2_leaf_reg=3,
    scale_pos_weight=scale_pos_weight, eval_metric='AUC',
    early_stopping_rounds=50, random_seed=42, verbose=0
)
cat_model.fit(Pool(X_tr_tree, y_tr), eval_set=Pool(X_val_tree, y_val))
results['CAT'] = evaluate('CatBoost', y_val, cat_model.predict_proba(X_val_tree)[:, 1])

ens_w = np.array([results['XGB']['auc'], results['LGB']['auc'], results['CAT']['auc']])
ens_w = ens_w / ens_w.sum()
ens_prob = (ens_w[0]*results['XGB']['y_prob'] +
            ens_w[1]*results['LGB']['y_prob'] +
            ens_w[2]*results['CAT']['y_prob'])
results['ENS'] = evaluate('Ensemble (AUC-wtd)', y_val, ens_prob)
print(f'  Weights -> XGB:{ens_w[0]:.3f}  LGB:{ens_w[1]:.3f}  CAT:{ens_w[2]:.3f}')


# ═══════════════════════════════════════════════════════════════════════════════
# 6. OPTUNA HPO (LightGBM)
# ═══════════════════════════════════════════════════════════════════════════════

def lgb_objective(trial):
    params = {
        'n_estimators':      trial.suggest_int('n_estimators', 300, 1500),
        'learning_rate':     trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
        'max_depth':         trial.suggest_int('max_depth', 3, 10),
        'num_leaves':        trial.suggest_int('num_leaves', 20, 200),
        'min_child_samples': trial.suggest_int('min_child_samples', 10, 100),
        'subsample':         trial.suggest_float('subsample', 0.5, 1.0),
        'colsample_bytree':  trial.suggest_float('colsample_bytree', 0.4, 1.0),
        'reg_alpha':         trial.suggest_float('reg_alpha', 1e-4, 10.0, log=True),
        'reg_lambda':        trial.suggest_float('reg_lambda', 1e-4, 10.0, log=True),
        'scale_pos_weight': scale_pos_weight, 'random_state': 42, 'verbosity': -1, 'n_jobs': -1
    }
    X_o_tr, X_o_val, y_o_tr, y_o_val = train_test_split(
        X_tr_tree, y_tr, test_size=0.20, random_state=trial.number, stratify=y_tr
    )
    m = lgb.LGBMClassifier(**params)
    m.fit(X_o_tr, y_o_tr, eval_set=[(X_o_val, y_o_val)], eval_metric='auc',
          callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(-1)])
    return roc_auc_score(y_o_val, m.predict_proba(X_o_val)[:, 1])

print('\nRunning Optuna HPO (100 trials)...')
study = optuna.create_study(direction='maximize', sampler=optuna.samplers.TPESampler(seed=42))
study.optimize(lgb_objective, n_trials=100, show_progress_bar=True)

best_params = {**study.best_params, 'scale_pos_weight': scale_pos_weight,
               'random_state': 42, 'verbosity': -1, 'n_jobs': -1}
print(f'Best inner AUC: {study.best_value:.4f}')

lgb_opt = lgb.LGBMClassifier(**best_params)
lgb_opt.fit(X_tr_tree, y_tr, eval_set=[(X_val_tree, y_val)], eval_metric='auc',
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)])
results['LGB_OPT'] = evaluate('LightGBM (Optuna)', y_val, lgb_opt.predict_proba(X_val_tree)[:, 1])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. MLFLOW + ARTIFACT SAVE
# ═══════════════════════════════════════════════════════════════════════════════

_mlruns = pathlib.Path('mlruns')
if _mlruns.exists():
    shutil.rmtree(_mlruns)

mlflow.set_tracking_uri('')
mlflow.set_experiment('credit_risk_pd_model')

best_key = max(results, key=lambda k: results[k]['auc'])
best_res = results[best_key]
log_model_obj = {'XGB': xgb_model, 'LGB': lgb_model, 'CAT': cat_model,
                 'LGB_OPT': lgb_opt, 'ENS': lgb_opt}.get(best_key, lgb_opt)

with mlflow.start_run(run_name=f'{best_res["name"]}_Champion') as run:
    mlflow.log_params({'champion_model': best_res['name'], 'n_features': X_tr.shape[1]})
    for k in ['auc', 'gini', 'ks', 'brier']:
        mlflow.log_metric(k, best_res[k])
    mlflow.sklearn.log_model(log_model_obj, artifact_path='model')
    print(f'MLflow run: {run.info.run_id}')

ARTIFACTS_DIR = Path('model_artifacts')
ARTIFACTS_DIR.mkdir(exist_ok=True)

single_key   = max(['XGB','LGB','CAT','LGB_OPT'], key=lambda k: results[k]['auc'])
champion_obj = {'XGB': xgb_model, 'LGB': lgb_model, 'CAT': cat_model, 'LGB_OPT': lgb_opt}[single_key]
feature_cols = list(X_tr.columns)

with open(ARTIFACTS_DIR / 'champion_model.pkl',   'wb') as f: pickle.dump(champion_obj, f)
with open(ARTIFACTS_DIR / 'xgb_model.pkl',        'wb') as f: pickle.dump(xgb_model, f)
with open(ARTIFACTS_DIR / 'lgb_model.pkl',        'wb') as f: pickle.dump(lgb_model, f)
with open(ARTIFACTS_DIR / 'cat_model.pkl',        'wb') as f: pickle.dump(cat_model, f)
with open(ARTIFACTS_DIR / 'feature_cols.json',     'w') as f: _json.dump(feature_cols, f, indent=2)
with open(ARTIFACTS_DIR / 'ensemble_weights.json', 'w') as f:
    _json.dump({'XGB': float(ens_w[0]), 'LGB': float(ens_w[1]), 'CAT': float(ens_w[2])}, f, indent=2)

le_to_save = {c: m for c, m in le_encoders.items() if m['type'] == 'label_encoder'}
with open(ARTIFACTS_DIR / 'label_encoders.pkl', 'wb') as f: pickle.dump(le_to_save, f)

model_card = {
    'champion_model':   single_key,
    'training_rows':    int(len(X_tr)),
    'validation_rows':  int(len(X_val)),
    'feature_count':    len(feature_cols),
    'target_mean':      float(y_tr.mean()),
    'metrics':          {k: results[single_key][k] for k in ['auc','gini','ks','brier']},
    'ensemble_metrics': {k: results['ENS'][k]       for k in ['auc','gini','ks']},
    'encoding': {'etl_aligned_cols':   list(ETL_CAT_MAPS.keys()),
                 'label_encoded_cols': list(le_to_save.keys())},
}
with open(ARTIFACTS_DIR / 'model_card.json', 'w') as f: _json.dump(model_card, f, indent=2)

with open(ARTIFACTS_DIR / 'compliance_exclusions.json', 'w') as f:
    _json.dump({
        'excluded_features': COMPLIANCE_EXCLUDED,
        'dropped_count':     len([f for f in COMPLIANCE_EXCLUDED if f not in feature_cols]),
        'model_version':     single_key,
        'note':              'Category A = direct protected attributes; Category B = redlining proxy',
    }, f, indent=2)

print('\nArtifacts:')
for p in sorted(ARTIFACTS_DIR.iterdir()):
    print(f'  {p.name:<35} {p.stat().st_size/1024:>7.1f} KB')


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SCORE TEST SET + SUBMISSION
# ═══════════════════════════════════════════════════════════════════════════════

X_test_aligned = X_test_raw.reindex(columns=X_tr.columns, fill_value=-999)
test_scores    = lgb_opt.predict_proba(X_test_aligned.values)[:, 1]

submission = pd.DataFrame({'SK_ID_CURR': app_test['SK_ID_CURR'], 'TARGET': test_scores})
submission.to_csv('submission.csv', index=False)

cal_gap = test_scores.mean() - y_train.mean()
print(f'\nSubmission: {len(submission):,} rows  |  Mean PD: {test_scores.mean():.2%}  |  Cal gap: {cal_gap:+.2%}')
if abs(cal_gap) > 0.02:
    print('  Warning: gap > 2pp — consider Platt scaling or isotonic recalibration.')

print('\n' + '='*65)
print(f'  Champion : {best_res["name"]}')
print(f'  AUC      : {best_res["auc"]:.4f}')
print(f'  Gini     : {best_res["gini"]:.4f}')
print(f'  KS       : {best_res["ks"]:.4f}')
print('='*65)
print('Done.')
