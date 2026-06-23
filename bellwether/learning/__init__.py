"""The self-learning loop.

Trading stays on its 15-minute cadence; learning happens in a separate, slower
*reflection* loop (daily by default). The pieces:

  • ``journal``    — log every signal the engine produced, then score it against
                     what actually happened. You can't learn from mistakes you
                     never wrote down.
  • ``reliability``— turn that track record into a bounded trust multiplier per
                     (strategy, coin): quietly down-weight chronically-wrong
                     sources, up-weight accurate ones.
  • ``memory``     — feed the model its own scorecard and let it write short
                     lessons (a trading journal) that get injected into tomorrow's
                     prompt.
  • ``discovery``  — search out new coins with a liquidity floor; new coins enter
                     on probation (tiny size) and graduate or retire on evidence.
  • ``autotune``   — nudge *selection* aggressiveness within hard bounds.
                     Capital-protection limits are human-owned and immutable.
  • ``reflect``    — the daily orchestrator that runs all of the above and
                     produces a human-readable summary for the email.

Design bias: slow learning beats fast overfitting to noise. Adaptations require
a minimum sample count and are regularized toward the prior.
"""
