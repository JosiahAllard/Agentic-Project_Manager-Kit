# Jev fit audit

Purpose: find the kit's routing and labelling decisions and say whether a structured-decision model could make them cheaper.

This is step 1 only: a desk study over 14 days of agent transcripts. There was no trial, because there is no TypeSafe account.

## What Jev is

Jev is TypeSafe AI's structured-decision model. It answers yes/no, pick-one-of and score questions. It is reached over an API. Third-party write-ups give a price of about $0.042 per million input tokens, with output free, and answers in 70 to 500 ms. No first-party price page or MCP server was found, so treat these figures as recalled from secondary sources, not checked.

## Decision steps found

The kit makes these choices inside agent turns. Counts and costs are measured from the transcripts at Claude list prices. Shares are of the 14-day Claude total of about $6,270.

| Decision step | Calls | Cost | Share | Output cost |
|---|---|---|---|---|
| Report check after a task notification | 9,785 | $515 | 8.2% | $108 |
| Cross-session message triage | 4,002 | $205 | 3.3% | $41 |
| Dispatch pick (role and model tier) | 2,575 | $177 | 2.8% | $89 |
| Hub post and routing | 1,846 | $81 | 1.3% | $17 |
| Record labelling (backlog writes) | 2,088 | $75 | 1.2% | $13 |
| Guard refusal handling | 1,441 | $33 | 0.5% | $3 |
| Permission denial handling | 166 | $6 | 0.1% | $1 |
| Total | 21,903 | $1,092 | 17.4% | $272 |

The rows can overlap: one turn can carry a notification and a message. The total is an upper bound.

How each step was found:

- A report check is a coordinator or PM call whose input carries a task notification.
- Triage is any call whose input carries a message from another session.
- A dispatch pick is a call that starts a subagent.
- Hub routing is a call to the hub tool or the hub server.
- Record labelling is the call that issues a backlog write.
- Refusal and denial handling is the call right after a refused tool call.

One more step sits outside the kit. Codex runs its own auto-review on each action: 2,835 calls averaging about 84,000 input tokens and 89 output tokens each. It costs about $53 over the period at an assumed rate. That is a classification step too, but Codex owns it, and the kit cannot change it.

## Where the money in a decision turn goes

Most of a decision turn's cost is not the decision. It is the agent re-reading its own context: cache reads and writes of everything it has seen so far. The decision itself is the output, about a quarter of the cost ($272 of $1,092).

A classifier can only replace the output. It cannot remove the context, because the agent still runs the turn.

## Saving estimate

### Jev inside the agent loop

The agent calls Jev as a tool and acts on the answer.

- Best case saved: the output share, $272.
- Added cost: each Jev answer is a tool result that the agent writes to its cache and re-reads on every later call. At the measured carry rate that is about $0.02 per call, or about $440 over 21,903 calls.
- Net: a loss of about $170, before any accuracy cost.

Assumptions: the Jev result plus the call is about 300 to 500 tokens. The agent lives on for the measured median number of calls. The output saved is all of it, which overstates the saving.

### Jev outside the loop, as a pre-filter

A small hook or relay asks Jev whether a notification or message needs the coordinator at all. Only then is it delivered.

- Target: report-check and triage turns that made no tool call, which means the agent read the news and did nothing. There were 2,848 report-check turns and 783 triage turns like this, costing $124 measured.
- Saving if Jev filters 50 to 80% of them correctly: $60 to $100 over 14 days.
- Jev's own cost: about 2,000 tokens per decision at $0.042 per million is under $0.0001 each, under $1 in total.

This assumes the turns that did nothing can be delivered later in a batch, or dropped. The assumption is an inference: some may have changed what the agent did next.

### Other steps

- Dispatch picks: a wrong role or tier changes the cost of a whole agent, often dollars, so a cheap pick is a false saving. Not recommended.
- Refusal and denial handling: $39 in total, and the kit guard already tells the agent the fix. Too small to matter.
- Hub routing and record labelling: $156 in total. The decision is bound up with writing the text, which Jev does not do.
- Codex auto-review: about $53 to about $10 at Jev input rates, but it is not the kit's to change.

## Risks

- Accuracy is unknown. Suppressing a report that says a gate failed is far more costly than the cents saved.
- Notification text holds repository content. Sending it off the machine needs the owner's say, and a filter that strips paths and names.
- The kit is standard-library Python only. An API call is possible with the standard library. An MCP server was not found.
- A rule needs no account. A plain pattern on "done, gate green" may capture most of the $124 by itself.

## What step 2 needs

1. A TypeSafe account and key. That is the owner's call.
2. A labelled set: a few hundred report-check, triage and refusal turns sampled from the transcripts. Label each by what the agent actually did next: acted, or only acknowledged. A read-only script can draw and pre-label the sample.
3. An accuracy bar fixed before the trial. A proposed default: no missed failure report in the sample, and at least half of the do-nothing turns caught.
4. A rule-based baseline on the same set, so Jev is compared with the cheap option, not with nothing.
5. A shadow week. Jev labels live notifications without acting on them, and the labels are compared with the coordinator's actions.
6. A data-handling check: exactly what text leaves the machine, with no personal details.

## Recommendation

Do not put Jev inside the agent loop. On the measured numbers it costs more than it saves.

The only fit is an out-of-loop notification pre-filter, worth at most about $124 per 14 days. Build and measure a rule-based filter first. Run step 2 only if the rule leaves a real gap.

This ranks below the listing trim, code-aware search, cache-gap and start-up work in the cost audit, each worth more and needing no outside service.
