# K Line Model Integration Plan

## Purpose

This project is an A-share research workstation, not an automatic trading or deterministic buy/sell system. A K line model can fit the product well if it is framed as research evidence:

- K line pattern reference
- historically similar price-volume windows
- model-backed probability reference
- backtest-supported signal context

It should not be presented as a certain prediction or direct trading instruction.

## Recommended Model Stack

## Pretrained Model Reality Check

There are pretrained time-series foundation models, such as TimesFM, Chronos, and Moirai. They can be useful for zero-shot or few-shot time-series forecasting experiments.

However, they are not the same thing as a ready-to-use A-share K line trading model.

Important limitations:

- they are usually trained for generic time-series forecasting, not specifically for A-share daily K line direction
- they may forecast price levels better than tradable direction or excess return
- they do not automatically understand A-share rules, limit-up/limit-down behavior, policy shocks, suspensions, board rotation, or local liquidity regimes
- they still need local backtesting before being shown in the product
- they may require heavier dependencies than LightGBM/XGBoost

Recommended use:

- treat pretrained models as an experiment lane
- compare them against simple baselines and Qlib/LightGBM before exposing them
- do not present zero-shot output as a trusted signal without local validation

### Kronos

Kronos deserves a separate experiment lane because it is built specifically for financial K line sequences rather than generic time series.

Reference and attribution:

- GitHub: https://github.com/shiyu-coder/Kronos
- Hugging Face models: https://huggingface.co/NeoQuasar
- Paper: https://arxiv.org/abs/2508.02739
- License: MIT License, Copyright (c) 2025 ShiYu

What makes it interesting:

- pretrained on financial candlestick data
- input format is directly compatible with OHLCV-style data
- supports zero-shot forecasting through a `KronosPredictor`
- provides public pretrained mini/small/base models
- includes a Qlib-based A-share finetuning and backtesting example

Recommended first use in this project:

- start with zero-shot `Kronos-mini` or `Kronos-small`
- use local daily OHLCV from SQLite
- forecast T+5 or T+10 daily bars
- convert predicted close path into a research reference, not a trading signal
- run rolling historical backtests before exposing the result in the UI

Integration cautions:

- the current project dependencies do not include PyTorch
- Kronos should be an optional model backend, not a required dependency for the whole app
- model weights need to be downloaded from Hugging Face or cached locally
- daily A-share inference needs proper future trading-day timestamps
- `amount` is optional, but if unavailable it should be filled consistently rather than invented
- zero-shot output should be compared against naive baselines before being trusted

Suggested backend shape:

```text
backend/ml/kronos_adapter.py
backend/ml/kronos_backtest.py

GET /api/predict/{symbol}/kronos-reference
GET /api/predict/{symbol}/kronos-backtest
```

The adapter should return:

- predicted T+5/T+10 close return
- probability or sample ratio of positive paths when `sample_count > 1`
- forecast path summary
- rolling backtest metrics
- clear research-only warning

Current implementation status:

- `backend/ml/kronos_adapter.py` has been added as an optional zero-shot adapter
- `GET /api/predict/{symbol}/kronos-reference` has been added
- `frontend/src/components/KLinePredictionPanel.tsx` has been added
- `.env.example` includes the required `KRONOS_*` settings
- rolling backtest and finetuning are still future work

### 1. Microsoft Qlib

Qlib is the best first candidate for this project because it is designed for quantitative research workflows, including data preparation, feature engineering, model training, evaluation, and backtesting.

Recommended first model:

- LightGBM or XGBoost
- Alpha158 or Alpha360 style factors
- targets such as T+1, T+3, T+5, T+10 forward return direction or rank score

Why it fits:

- close to the current backend ML style
- interpretable enough for a research workstation
- easier to backtest and compare against baselines
- suitable for offline training and cached inference

### 2. Nixtla MLForecast / NeuralForecast

These are good second-stage candidates for time-series experiments.

Suggested use:

- MLForecast for lightweight machine-learning time-series baselines
- NeuralForecast for TCN, NBEATS, NHITS, TFT, PatchTST, or iTransformer experiments

Why not first:

- they are stronger as generic time-series forecasting tools than complete financial research pipelines
- they require more careful target design and validation
- deep models may add complexity before the baseline is proven

### 3. sktime

sktime is useful for K line shape classification and time-series similarity tasks.

Suggested use:

- classify recent 10/20/60-day K line shapes
- find similar historical windows
- summarize post-window return distributions

This is especially useful if the feature is called "K line pattern reference" instead of "prediction".

### 4. FinRL

FinRL should be treated as a later-stage option. It is more suitable for reinforcement-learning trading agents and portfolio decision systems than for this project's current single-stock research workflow.

## Suggested Integration Architecture

### Offline Training

Do not train large models inside normal FastAPI request handlers.

Create an offline training command such as:

```bash
python -m backend.ml.train_kline_model --engine qlib --model lightgbm --symbols all
```

The training job should:

- read local SQLite OHLC and optional event features
- build price-volume/factor features
- split data by time, not randomly
- run walk-forward or expanding-window backtests
- save model artifacts and metadata under `backend/ml/models/`

### Model Metadata

Every saved model should include:

- model name and version
- feature set version
- training symbol universe
- training date range
- validation date range
- target definition
- baseline metric
- model metric
- sample size
- created time

The frontend should only show a model reference when the backtest beats the baseline by a configured minimum threshold.

### API Design

Recommended endpoints:

```text
GET /api/predict/{symbol}/kline-reference
GET /api/predict/{symbol}/kline-backtest
```

Example response:

```json
{
  "symbol": "sh600519",
  "model": "qlib_lightgbm_alpha158",
  "feature_version": "alpha158_v1",
  "as_of_date": "2026-05-15",
  "horizon": "t5",
  "direction_score": 0.62,
  "confidence_bucket": "medium",
  "verdict": "slightly_positive",
  "backtest": {
    "sample_size": 1200,
    "accuracy": 0.53,
    "baseline": 0.51,
    "lift": 0.02,
    "ic": 0.04
  },
  "top_factors": [
    {
      "name": "ret_20d",
      "value": 0.08,
      "direction": "positive"
    }
  ],
  "warning": "Research reference only. Not investment advice."
}
```

### Frontend Presentation

The UI should avoid strong wording such as:

- buy
- sell
- guaranteed
- must rise
- must fall

Better labels:

- K line model reference
- historical pattern statistics
- model observation
- backtest-supported tendency

## Model Validity Window

There is no universal fixed validity period for a stock model. In this project, the practical validity should be managed at two levels.

### Prediction Horizon

For daily K line models, the useful horizon is usually short:

- T+1: useful only for the next trading day
- T+3 to T+5: often the most practical short-term reference window
- T+10 to T+20: should be treated as scenario tendency, not precise direction
- beyond 20 trading days: better handled by fundamentals, industry, macro, and event research

For this project, T+3 and T+5 should be the first serious targets. T+1 is noisy, and T+20 can easily become too vague.

### Model Shelf Life

After training, a model should not be trusted forever. Market style changes, policy cycles, liquidity conditions, and sector rotations can invalidate learned relationships.

Suggested refresh rules:

- normal use: retrain weekly or monthly
- active research mode: retrain after each major data update
- market regime change: retrain immediately
- model monitoring: disable or downgrade the model if recent live performance falls below baseline

Practical initial rule:

- retrain every 1 month
- run backtest report every retrain
- monitor rolling 20/60-trading-day live accuracy or IC
- hide the model reference if it stops beating baseline

## Resource Requirements

## Training Data and Time Estimate

### Data Needed

Minimum useful data depends on the target.

For a single-stock daily K line model:

- absolute minimum: 2 to 3 years of daily bars, roughly 500 to 750 trading days
- better: 5 to 10 years, roughly 1200 to 2400 trading days
- still risky: single-stock-only training can overfit badly

For a cross-sectional A-share model:

- better approach: train across many stocks and many dates
- small research universe: 50 to 200 stocks
- stronger baseline universe: 500 to 3000 stocks
- useful sample size: tens of thousands to millions of stock-day rows

The first production-like version should use cross-sectional training if possible. It can still serve single-stock references at inference time.

### Training Time

Approximate first-stage LightGBM/XGBoost time:

- one stock, 5 to 10 years daily data: seconds
- 50 to 200 stocks with daily factors: minutes
- 500 to 3000 stocks with many factors and walk-forward validation: tens of minutes to a few hours, depending on feature cache and CPU

Deep time-series models:

- one-stock experiment: minutes to tens of minutes
- multi-stock experiments: hours are normal
- repeated tuning: GPU becomes useful quickly

Pretrained time-series foundation models:

- no full training needed for zero-shot inference
- local fine-tuning, if used, still needs GPU or substantial CPU time
- backtesting still takes time because every historical prediction window must be replayed

Practical first benchmark:

- use 5 to 10 years of daily data
- start with 50 to 200 liquid A-share names
- train LightGBM/XGBoost offline
- run walk-forward validation
- only expose the model if it beats baseline after costs and sample checks

### Lightweight Baseline

Recommended first phase:

- model: LightGBM or XGBoost
- data: daily OHLCV plus existing event/news features
- CPU: normal laptop or small server is enough
- memory: 4 GB to 8 GB usually enough for a small A-share universe
- GPU: not required
- training time: seconds to minutes for a small universe, longer for full market
- inference: very cheap, can run inside FastAPI after loading cached artifacts

This is the best fit for the current project.

### Qlib Workflow

Qlib adds more data and experiment-management structure.

Expected needs:

- CPU: 4 cores or more preferred
- memory: 8 GB minimum, 16 GB better
- disk: several GB to tens of GB depending on stock universe and feature cache
- GPU: not needed for LightGBM/XGBoost
- training mode: offline scheduled job
- serving mode: cached model artifacts loaded by backend API

### Deep Time-Series Models

LSTM, TCN, PatchTST, TFT, and similar models require more resources and stricter validation.

Expected needs:

- CPU-only is possible for small experiments, but slow
- GPU is recommended for repeated experiments
- memory: 16 GB or more preferred
- more careful hyperparameter tuning
- higher overfitting risk on single-stock data

These should be second-stage experiments after the LightGBM/Qlib baseline proves useful.

## Recommended First Milestone

1. Add a `kline-reference` backend endpoint.
2. Use current SQLite OHLC data and existing feature engineering first.
3. Train a LightGBM/XGBoost baseline with walk-forward validation.
4. Save model artifacts and metadata.
5. Show only:
   - direction tendency
   - confidence bucket
   - sample size
   - model vs baseline
   - top factors
6. Add a clear research-only warning.

This gives the project a useful K line model layer without turning it into an unreliable trading signal product.
