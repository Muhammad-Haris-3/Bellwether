import Link from "next/link";
import { ScrollReveal } from "../components/ScrollReveal";

// Written for someone who has never heard of any of this.
//
// No jargon in the first three paragraphs, no metric names above the fold, and
// nothing that requires knowing what PR-AUC is to understand what the project
// does or why it exists. The technical pages are one click away and say the
// rest.

export const metadata = {
  title: "About — Bellwether",
  description:
    "What this project is, why it exists, and what the numbers on it mean.",
};

export default function AboutPage() {
  return (
    <main>
      <nav className="crumbs">
        <Link href="/">Status</Link>
        <Link href="/timeline">Timeline</Link>
        <Link href="/metrics">Performance</Link>
        <Link href="/queue">Queue</Link>
        <span aria-current="page">About</span>
      </nav>

      <ScrollReveal>
        <h1>What this is</h1>
        <p className="lead">
          Anyone can edit Wikipedia, and most people who do are trying to help. A
          few are not. A small group of volunteers reads the incoming edits
          looking for damage — and there are far more edits than there are people
          to read them.
        </p>
        <p>
          Bellwether watches those edits as they arrive and guesses which ones are
          likely to be undone. The guesses go in a queue, worst first, so a
          volunteer&rsquo;s attention lands where it is most likely to be needed.
        </p>
      </ScrollReveal>

      <ScrollReveal>
        <h2>Why it exists</h2>
        <p>
          The interesting part is not the guessing. Plenty of systems guess.
        </p>
        <p>
          The problem with a system like this is that it quietly stops working.
          The people it watches change how they behave, and the model keeps
          answering with the same confidence it always had. Almost every deployed
          model is tested once, before it goes live, and then trusted forever
          after. Nobody checks whether it is still right. Nobody decides in
          advance what &ldquo;no longer right&rdquo; would even look like.
        </p>
        <p className="lead">
          So this project asks a different question:{" "}
          <strong>
            can a system notice it is getting worse, fix itself, and be trusted to
            have told the truth about it?
          </strong>
        </p>
      </ScrollReveal>

      <ScrollReveal>
        <h2>How it keeps itself honest</h2>
        <p>
          The whole design is built around one problem: it is very easy to fool
          yourself about how well something works, especially when you built it.
          Four rules do the work.
        </p>
        <div className="about-grid">
          <div className="about-card">
            <h3>It commits before it finds out</h3>
            <p>
              Every guess is written down the moment an edit appears —{" "}
              <em>hours or days before</em> anyone knows what happened to it. The
              record cannot be edited or deleted afterwards, not even by the
              people who run it. The database itself refuses.
            </p>
          </div>

          <div className="about-card">
            <h3>It waits for the answer</h3>
            <p>
              An edit that has not been undone yet has not survived — nobody has
              looked at it. Guesses only count towards a score once enough time
              has passed that the outcome is genuinely settled. A week, in
              practice.
            </p>
          </div>

          <div className="about-card">
            <h3>The rules were written first</h3>
            <p>
              Before the first model existed, the exact conditions for replacing
              it were written down and published: how much better a replacement
              must be, how much evidence is required, how long it must be watched.
              Those numbers cannot be adjusted afterwards to make a result look
              better.
            </p>
          </div>

          <div className="about-card">
            <h3>It reports the bad news too</h3>
            <p>
              Every rejected replacement is published alongside the accepted ones.
              So is the least flattering thing the evaluation found, and the
              comparison against Wikimedia&rsquo;s own model — which is expected
              to win, and that expectation was recorded in advance.
            </p>
          </div>
        </div>
      </ScrollReveal>

      <ScrollReveal>
        <h2>What runs without anyone watching</h2>
        <p>
          Every ten minutes it collects new edits and scores them. Every few hours
          it grades its old guesses against what actually happened. Every day it
          checks whether it has got worse, and if it has, it trains a replacement,
          runs that replacement silently alongside the real one for a week, and
          then either promotes it or refuses to — by the rules written down
          beforehand. If a promotion turns out badly, it puts the old one back on
          its own.
        </p>
        <p>
          Nobody approves any of that. The record of what it decided, and why, is
          on the <Link href="/timeline">timeline</Link>.
        </p>
      </ScrollReveal>

      <ScrollReveal>
        <h2>What the pages show you</h2>
        <table>
          <tbody>
            <tr>
              <td>
                <Link href="/">Status</Link>
              </td>
              <td>
                What it has collected, and whether the machinery is still running.
                If a job has stopped, this says so rather than showing you a stale
                number.
              </td>
            </tr>
            <tr>
              <td>
                <Link href="/metrics">Performance</Link>
              </td>
              <td>
                How accurate it has actually been — with the sample size next to
                every figure, because a number without one cannot be judged.
              </td>
            </tr>
            <tr>
              <td>
                <Link href="/timeline">Timeline</Link>
              </td>
              <td>
                Every decision it made about replacing its own model, including
                the refusals, with the evidence.
              </td>
            </tr>
            <tr>
              <td>
                <Link href="/queue">Queue</Link>
              </td>
              <td>
                The live triage list. Sign-in required — accounts are issued by
                hand.
              </td>
            </tr>
          </tbody>
        </table>
      </ScrollReveal>

      <ScrollReveal>
        <h2>How to read the numbers</h2>
        <p className="lead">
          Every figure on the Performance page is three things: a score, something
          to compare it against, and a range. The score on its own is the least
          useful of the three.
        </p>

        <div className="about-grid">
          <div className="about-card">
            <h3>1. A score alone means nothing</h3>
            <p>
              You will see numbers like <code>0.22</code>. That is not a
              percentage and it is not a grade. Bad edits are rare &mdash; about
              nine in a hundred here &mdash; and finding rare things is hard, so
              even a good ranker scores low on this scale. Ignore it until you
              have something to hold it against.
            </p>
          </div>
          <div className="about-card">
            <h3>2. The gap is the answer</h3>
            <p>
              Every score sits beside a comparison, and the difference between
              them is the only part that answers a question. Positive means this
              system did better than the thing it was compared with. Negative
              means it did worse. That one number is what to read.
            </p>
          </div>
          <div className="about-card">
            <h3>3. The bracket says whether to believe it</h3>
            <p>
              Each gap carries a range in brackets. If the whole range sits on one
              side of zero, the result is real. If the range crosses zero, the
              honest answer is <em>too close to call</em> &mdash; however good the
              middle number looks. This is the step most people skip.
            </p>
          </div>
        </div>

        <h3>Worked example</h3>
        <p>
          Here are the two comparisons as they stood on 26 August 2026, read the
          way described above. The live versions are on the{" "}
          <Link href="/metrics">Performance</Link> page.
        </p>
        <table>
          <thead>
            <tr>
              <th>Compared against</th>
              <th>Gap</th>
              <th>Range</th>
              <th>How to read it</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                A simple rule &mdash; &ldquo;suspect anyone not logged in&rdquo;
              </td>
              <td className="ok">+0.052</td>
              <td>
                <code>+0.047 to +0.057</code>
              </td>
              <td>
                The whole range is above zero, so this is real. The model earns
                its place: it beats the obvious shortcut, and not by luck.
              </td>
            </tr>
            <tr>
              <td>
                Wikipedia&rsquo;s own model, on the same edits
              </td>
              <td className="warn">&minus;0.014</td>
              <td>
                <code>&minus;0.092 to +0.079</code>
              </td>
              <td>
                The range crosses zero, so this is a draw. Theirs is slightly
                ahead in the middle, but not by enough to claim anything either
                way yet.
              </td>
            </tr>
          </tbody>
        </table>
        <p className="note">
          The second row is the one the project cares about most, and it was
          predicted in writing before any model existed here: Wikipedia&rsquo;s
          production model was expected to win. It is published whichever way it
          falls, and right now it falls on &ldquo;level, on the evidence so
          far&rdquo;.
        </p>

        <h3>So is it any good?</h3>
        <p>
          On the evidence above: yes, with a limit. It clearly beats the simple
          shortcut, which is the bar that decides whether the model is worth
          running at all. Against a professionally maintained model it is
          currently level rather than ahead &mdash; and the sample is still small
          enough that this could move in either direction.
        </p>
        <p>
          Two habits will keep you from misreading anything here. Check the sample
          size beside a figure before trusting it, and check the range before
          repeating it. A number measured on a few hundred events is a hint, not a
          finding.
        </p>

        <div className="callout">
          <strong>A red job is not always bad news.</strong> On the{" "}
          <Link href="/">Status</Link> page some jobs are designed to fail loudly.
          The one that checks whether the stored numbers can be rebuilt from
          scratch refuses to quietly correct what it finds, because silently
          fixing a problem removes the only evidence that something is causing it.
          Red there means &ldquo;a difference was found and is being reported&rdquo;,
          not &ldquo;the site is broken&rdquo;. What matters is whether the number
          beside it is falling.
        </div>
      </ScrollReveal>

      <ScrollReveal>
        <h2>One thing worth knowing before you read a number</h2>
        <p>
          This system treats &ldquo;the edit was undone&rdquo; as meaning
          &ldquo;the edit was bad&rdquo;. That is not quite true. Good edits get
          undone by mistake, and bad ones survive because nobody noticed. Every
          figure here rests on that substitution.
        </p>
        <p>
          Rather than assume it away, the project is measuring it: reviewers judge
          edits without being shown the model&rsquo;s opinion, and their verdicts
          are compared against what actually happened. That study is running now
          and does not have an answer yet. When it does, the answer will be
          published whichever way it comes out.
        </p>
      </ScrollReveal>

      <ScrollReveal>
        <h2>Honest limits</h2>
        <ul>
          <li>
            It runs entirely on free infrastructure, so the server sleeps when
            idle and the first page load can take up to a minute.
          </li>
          <li>
            It reads a fixed sample of edits rather than all of them, and corrects
            for that sample in every figure.
          </li>
          <li>
            It never edits Wikipedia. It only reads, ranks, and reports.
          </li>
          <li>
            It is one person&rsquo;s project, built in public, and everything
            above is verifiable from the source.
          </li>
        </ul>
      </ScrollReveal>

      <footer>
        <a href="https://github.com/Muhammad-Haris-3/Bellwether">Source</a>
        <span>
          <Link href="/">Status</Link> · <Link href="/metrics">Performance</Link>{" "}
          · <Link href="/timeline">Timeline</Link>
        </span>
      </footer>
    </main>
  );
}
