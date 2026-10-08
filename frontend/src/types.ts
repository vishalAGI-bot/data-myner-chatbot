import type { JourneyResponse, ThinkingStep } from "./api/journey";
import type { AnalystResponse } from "./api/analyst";

export type AgentId = "analyst" | "journey";

export interface AgentInfo {
  id: AgentId;
  letter: string;
  name: string;
  blurb: string;
  subtitle: string;
  caption: string;
  placeholder: string;
  suggestions: string[];
}

export const AGENTS: Record<AgentId, AgentInfo> = {
  analyst: {
    id: "analyst",
    letter: "A",
    name: "Data analyst",
    blurb: "All tables: journeys, purchases, sessions, users",
    subtitle: "Picks the right tables for each question · read-only",
    caption: "answered by the data analyst",
    placeholder: "Ask about purchases, leakage, apps, sessions, users, categories…",
    suggestions: [
      "What's Amazon's overall purchase share, and who is ahead of us?",
      "Of all journeys that start on Amazon, what share end up buying on Amazon vs. somewhere else?",
      "When users search on Amazon and then leave, which categories drive most of that leakage?",
      "What's our average time-to-purchase compared to Meesho and Flipkart?",
      "For users who go Amazon → Meesho, what are they searching for right before they switch?",
      "Does our retention rate differ by device type or time of day?",
      "Which competitor do Meesho users switch to?",
    ],
  },
  journey: {
    id: "journey",
    letter: "J",
    name: "Journey agent",
    blurb: "Live cross-app journeys from PostgreSQL",
    subtitle: "Answers from live cross_app_journey data · read-only",
    caption: "answered by the journey agent",
    placeholder: "Ask about journeys, users, apps, searches or outcomes…",
    // Journey suggestions come from the loaded data summary, not this UI bundle.
    suggestions: [],
  },
};

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  error?: boolean;
  journey?: JourneyResponse;
  analyst?: AnalystResponse;
  thinking?: ThinkingStep[];
}
