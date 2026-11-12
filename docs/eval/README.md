# Evaluation results

Each file here is the output of one `python -m evals.run` against the LLM configured at the time,
named after the model that was requested. The report records the endpoint, the model the endpoint
*reported* serving, and the adapter settings, so results from different providers aren't confused.

How to read a report: *grounded* documents were processed automatically; the rest were routed to a
human (a cost, not an error). **Silently wrong** - auto-processed and reached the wrong outcome - is
the safety metric; it must be zero.
