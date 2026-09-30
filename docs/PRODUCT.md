# Product Specification: Roadside Assistance Triage AI Voice Agent (MVP)

## 1. The "Why" & Problem Statement
Stranded drivers requiring roadside towing assistance experience high anxiety and frustrating hold times during peak hours, adverse weather events, or late-night shifts. Simultaneously, towing dispatchers are overwhelmed with manual, repetitive intake questions (location, vehicle details, breakdown reason). This administrative bottleneck pulls their attention away from managing active tow fleets, tracking drivers, and handling high-risk emergency situations.

This MVP introduces an autonomous AI voice triage agent that instantly answers incoming non-emergency roadside assistance calls. The agent systematically collects critical breakdown details, stores structured assistance requests in a database, and alerts dispatchers without human intervention—allowing dispatchers to focus purely on logistics and routing.

## 2. Target Personas
- **Primary User (The Dispatcher)**: Towing company dispatchers who need structured, pre-vetted assistance request data delivered instantly to their queue without having to answer basic intake calls.
- **Secondary User (The Stranded Driver)**: Motorists stranded on the roadside who need immediate, calm, and efficient intake without waiting on hold, providing them reassurance that help is on the way.

## 3. Core Features (MVP Scope)
1. **Autonomous Voice Intake**: 24/7 inbound PSTN phone call answering. Twilio receives the call and streams its audio to the OpenAI Realtime API over a WebSocket bridge hosted by the backend, which conducts the conversation and hangs up (or redirects) when done.
2. **Structured 3-Step Triage**: Progressive intake that collects exactly three variables:
   - **Location**: Cross-streets, landmarks, or highway mile markers.
   - **Vehicle Details**: Make, model, and color.
   - **Issue Nature**: The specific mechanical problem (e.g., flat tire, engine smoke, dead battery).
   Details are saved as the caller provides them (partial saves allowed, nothing invented), then read back for verbal confirmation; the request completes only on a clear affirmative summary confirmation.
3. **Emergency Escalation**: The agent assesses whether the caller is in immediate physical danger (e.g., active fire, collision with injuries, vehicle stopped in active traffic lanes) and silently invokes an emergency-transfer tool — benign word mentions alone do not trigger it. The system then speaks a fixed transfer message and redirects the call to the configured emergency destination. Demo deployments hang up after the message instead of dialing out (default).
4. **Automated Assistance-Request Persistence**: Realtime function-tool calls from the voice model are validated and merged into a PostgreSQL (Supabase) assistance request, with completion, abandonment, and escalation tracked as lifecycle states.
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