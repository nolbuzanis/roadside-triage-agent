# Product Specification: Roadside Assistance Triage AI Voice Agent (MVP)

## 1. The "Why" & Problem Statement
Stranded drivers requiring roadside towing assistance experience high anxiety and frustrating hold times during peak hours, adverse weather events, or late-night shifts. Simultaneously, towing dispatchers are overwhelmed with manual, repetitive intake questions (location, vehicle details, breakdown reason). This administrative bottleneck pulls their attention away from managing active tow fleets, tracking drivers, and handling high-risk emergency situations.

This MVP introduces an autonomous AI voice triage agent that instantly answers incoming non-emergency roadside assistance calls. The agent systematically collects critical breakdown details, stores structured assistance requests in a database, and alerts dispatchers without human intervention—allowing dispatchers to focus purely on logistics and routing.

## 2. Target Personas
- **Primary User (The Dispatcher)**: Towing company dispatchers who need structured, pre-vetted assistance request data delivered instantly to their queue without having to answer basic intake calls.
- **Secondary User (The Stranded Driver)**: Motorists stranded on the roadside who need immediate, calm, and efficient intake without waiting on hold, providing them reassurance that help is on the way.

## 3. Core Features (MVP Scope)
1. **Autonomous Voice Intake**: 24/7 inbound PSTN phone call answering powered by Vapi (integrating Twilio SIP, Deepgram Nova-2 STT, OpenAI GPT-4o, and ElevenLabs TTS).
2. **Structured 3-Step Triage**: A strict conversational state machine that extracts exactly three variables:
   - **Location**: Cross-streets, landmarks, or highway mile markers.
   - **Vehicle Details**: Make, model, and color.
   - **Issue Nature**: The specific mechanical problem (e.g., flat tire, engine smoke, dead battery).
3. **Emergency & Hazard Escalation**: Real-time detection of high-hazard keywords (e.g., "active traffic", "fire", "smoke", "hurt", "bleeding"). Upon detection, the agent immediately interrupts the standard flow and transfers the call to 911 or a live human operator.
4. **Automated Assistance-Request Persistence**: Real-time webhook integration that parses structured function-call JSON payloads from the LLM and inserts them into a PostgreSQL database.
5. **Dispatcher SMS Notification**: Instant SMS alert sent to the on-duty dispatcher's phone via Twilio upon successful intake, containing the formatted assistance request data.

## 4. Explicit Non-Goals (Out of Scope for MVP)
To ensure a rapid MVP launch, the following features are strictly excluded:
- **Payment Processing**: No credit card collection, quoting, or invoice generation over the phone.
- **Dynamic Routing/Scheduling**: The agent will not automatically dispatch a specific truck or calculate driver ETAs.
- **GPS Integration**: No reliance on mobile device location sharing (intake relies entirely on verbal descriptions).
- **Outbound Calling**: The system only handles inbound calls; it does not call drivers back if they hang up.
- **Multilingual Support**: The MVP operates exclusively in English.

## 5. Key Success Metrics (KPIs)
- **Call Deflection Rate**: Percentage of inbound calls successfully converted into a complete database assistance request without human intervention (Target: >70%).
- **Time-to-Intake**: Average duration of a completed AI call (Target: < 90 seconds).
- **Escalation Accuracy**: Zero false negatives for high-hazard keywords (100% of hazard calls correctly transferred to a human).

## 6. Guiding Principle
*Zero hold time, frictionless structured intake, and absolute safety through immediate human escalation when required.*