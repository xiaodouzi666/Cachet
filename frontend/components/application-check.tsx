import type { ApplicationCheck, ApplicationEvidence } from "../lib/types";
import { Tag } from "./ui";

function CheckReceipt({ check }: { check?: ApplicationCheck | null }) {
  const { http, answer_sha256, source_sha256 } = check || {};
  if (
    !http &&
    !answer_sha256 &&
    !source_sha256 &&
    typeof check?.requested_format_met !== "boolean"
  )
    return null;
  return (
    <details className="check-receipt">
      <summary>Inspect verification evidence</summary>
      {typeof check?.requested_format_met === "boolean" && (
        <p>
          Requested response format ·{" "}
          {check.requested_format_met ? "Matched" : "Did not match"}
        </p>
      )}
      {answer_sha256 && (
        <p>
          Answer SHA-256 <code>{answer_sha256}</code>
        </p>
      )}
      {source_sha256 && (
        <p>
          Module SHA-256 <code>{source_sha256}</code>
        </p>
      )}
      {http != null && (
        <>
          <small>Observed HTTP responses</small>
          <pre className="receipt">{JSON.stringify(http, null, 2)}</pre>
        </>
      )}
    </details>
  );
}

export function GeneratedApplication({
  evidence,
  finalTurn,
  answerLink,
  finalResponse,
  earlierResponse,
}: {
  evidence: ApplicationEvidence;
  finalTurn?: number;
  answerLink?: (turn: number) => string;
  finalResponse?: string;
  earlierResponse?: string;
}) {
  const final = evidence.application_check;
  const selected = evidence.verified_application_turn;
  const prior =
    final?.status !== "passed" &&
    evidence.earlier_application_check?.status === "passed" &&
    typeof selected === "number" &&
    selected > 0 &&
    (finalTurn === undefined || selected < finalTurn)
      ? evidence.earlier_application_check
      : null;
  const status = final?.status || "unverified";
  return (
    <section
      className="application-check"
      aria-label="Generated application check"
    >
      <h3>Generated application check</h3>
      <div className="application-result">
        <div className="toolbar">
          <strong>
            Final response{finalTurn ? ` · Turn ${finalTurn}` : ""}
          </strong>
          <Tag
            tone={
              status === "passed"
                ? "good"
                : status === "failed"
                  ? "bad"
                  : "neutral"
            }
          >
            {status === "passed"
              ? "Passed"
              : status === "failed"
                ? "Failed"
                : "Unverified"}
          </Tag>
        </div>
        {final?.reason ? (
          <p>{final.reason}</p>
        ) : status === "unverified" ? (
          <p>
            This run has no recorded execution result for the final response.
          </p>
        ) : null}
        {answerLink && finalTurn && (
          <a className="text-button" href={answerLink(finalTurn)}>
            Read final response
          </a>
        )}
        {final?.requested_format_met === false && (
          <p>The response did not match the requested format.</p>
        )}
        {finalResponse && (
          <details>
            <summary>Read original final response</summary>
            <pre className="receipt">{finalResponse}</pre>
          </details>
        )}
        <CheckReceipt check={final} />
      </div>
      {prior && selected != null && (
        <div className="application-result">
          <div className="toolbar">
            <strong>Earlier module · Turn {selected}</strong>
            <Tag tone="good">Passed</Tag>
          </div>
          {answerLink && (
            <a className="text-button" href={answerLink(selected)}>
              Read turn {selected} response
            </a>
          )}
          {prior.requested_format_met === false && (
            <p>The response did not match the requested format.</p>
          )}
          {earlierResponse && (
            <details>
              <summary>Read earlier response</summary>
              <pre className="receipt">{earlierResponse}</pre>
            </details>
          )}
          <CheckReceipt check={prior} />
        </div>
      )}
    </section>
  );
}
