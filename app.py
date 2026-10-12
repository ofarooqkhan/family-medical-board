import streamlit as st
import os

if os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.environ["AI_INTEGRATIONS_OPENAI_API_KEY"]
if os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL"):
    os.environ["OPENAI_BASE_URL"] = os.environ["AI_INTEGRATIONS_OPENAI_BASE_URL"]

from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI
import json
import pandas as pd
import openai
from docx import Document as DocxDocument
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
import tempfile
from datetime import datetime
import io
import base64
from PIL import Image
import pydicom
from pydicom.pixel_data_handlers.util import apply_voi_lut
import numpy as np
import re

import database as db
from utils import parse_addressed_doctor, normalize_extracted_readings, dev_user_id
import streamlit.components.v1 as components

UPLOADS_DIR = "patient_data_uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

ACCESS_DENIED_MESSAGE = "You do not have access to this record."


def show_access_denied():
    st.error(ACCESS_DENIED_MESSAGE)

def get_replit_user():
    """Get user from Replit headers (works when deployed)"""
    headers = None
    try:
        headers = st.context.headers
    except:
        try:
            from streamlit.web.server.websocket_headers import _get_websocket_headers
            headers = _get_websocket_headers()
        except:
            pass

    if headers:
        user_id = headers.get('X-Replit-User-Id')
        user_name = headers.get('X-Replit-User-Name')
        user_profile = headers.get('X-Replit-User-Profile-Image')
        if user_id:
            return {
                'id': user_id,
                'name': user_name,
                'profile_image_url': user_profile
            }
    return None

def render_login_page():
    """Render login page"""
    st.markdown("""
    <style>
        .login-container {
            max-width: 400px;
            margin: 100px auto;
            padding: 40px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        .login-title {
            text-align: center;
            color: white;
            font-size: 28px;
            margin-bottom: 10px;
        }
        .login-subtitle {
            text-align: center;
            color: rgba(255,255,255,0.8);
            font-size: 14px;
            margin-bottom: 30px;
        }
    </style>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("## 🏥 Medical Board AI")
        st.markdown("Multi-specialist AI consultation system")
        st.divider()

        replit_user = get_replit_user()
        if replit_user:
            st.success(f"Welcome back, {replit_user.get('name', 'User')}!")
            if st.button("Continue to App", use_container_width=True, type="primary"):
                user = db.get_or_create_user(
                    replit_user['id'],
                    first_name=replit_user.get('name'),
                    profile_image_url=replit_user.get('profile_image_url')
                )
                db.migrate_orphan_patients_to_user(user['id'])
                st.session_state.user_id = user['id']
                st.session_state.user = user
                st.rerun()
        else:
            st.info("Sign in to access your patients and conversations")

            with st.form("login_form"):
                name = st.text_input("Your Name", placeholder="Dr. Smith")
                email = st.text_input("Email (optional)", placeholder="doctor@hospital.com")
                submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")

                if submitted and name:
                    # Deterministic id: the same name signs in to the same account (audit bug 2).
                    user_id = dev_user_id(name)
                    user = db.get_or_create_user(user_id, email=email, first_name=name)
                    migrated = db.migrate_orphan_patients_to_user(user['id'])
                    st.session_state.user_id = user['id']
                    st.session_state.user = user
                    st.rerun()
                elif submitted:
                    st.error("Please enter your name")

            st.caption("In production, users sign in via Google, GitHub, or other providers.")

def render_chat_sidebar(user_id):
    """Render ChatGPT-style chat history sidebar"""
    st.sidebar.markdown("### 💬 Conversations")

    if st.sidebar.button("+ New Chat", use_container_width=True, type="primary"):
        if st.session_state.get('current_patient_id'):
            try:
                conv_id = db.create_conversation(
                    user_id,
                    st.session_state.current_patient_id,
                    "New Consultation"
                )
                st.session_state.current_conversation_id = conv_id
                st.rerun()
            except PermissionError:
                show_access_denied()
        else:
            st.sidebar.warning("Select a patient first")

    st.sidebar.divider()

    conversations = db.get_user_conversations(user_id)

    if not conversations:
        st.sidebar.caption("No conversations yet. Start a new chat!")
    else:
        for conv in conversations:
            created = conv['created_at'].strftime('%b %d') if conv.get('created_at') else ''
            patient_name = conv.get('patient_name', 'Unknown Patient')
            title = conv.get('title', 'Conversation')[:30]

            is_active = st.session_state.get('current_conversation_id') == conv['id']

            col1, col2 = st.sidebar.columns([5, 1])
            with col1:
                btn_type = "primary" if is_active else "secondary"
                if st.button(f"📋 {title}", key=f"conv_{conv['id']}", use_container_width=True):
                    st.session_state.current_conversation_id = conv['id']
                    st.session_state.current_patient_id = conv['patient_id']
                    st.rerun()
            with col2:
                if st.button("🗑️", key=f"del_conv_{conv['id']}"):
                    try:
                        db.delete_conversation(conv['id'], user_id)
                        if st.session_state.get('current_conversation_id') == conv['id']:
                            st.session_state.current_conversation_id = None
                        st.rerun()
                    except PermissionError:
                        show_access_denied()

            st.sidebar.caption(f"  {patient_name} • {created}")

def get_api_key():
    return os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

def get_base_url():
    return os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")

def create_agents_from_db(api_key):
    """Create agents from database configurations"""
    base_url = get_base_url()
    llm = ChatOpenAI(model="gpt-4o", api_key=api_key, base_url=base_url, temperature=0.1)

    agents = {}
    db_agents = db.get_doctor_agents()

    for agent_config in db_agents:
        agents[agent_config['agent_type']] = Agent(
            role=agent_config['role'],
            goal=agent_config['goal'],
            backstory=agent_config['backstory'],
            llm=llm,
            verbose=True
        )

    return agents

def get_patient_context(patient_id, user_id):
    """Build patient context from database"""
    patient = db.get_patient(patient_id, user_id)
    if not patient:
        return "No patient selected."

    latest_vitals = db.get_latest_vitals(patient_id, user_id)
    memories = db.get_doctor_memories(patient_id, user_id=user_id)
    files = db.get_patient_files(patient_id, user_id)

    days_post_stroke = 0
    if patient.get('stroke_date'):
        days_post_stroke = (datetime.now() - patient['stroke_date']).days

    context = f"""
PATIENT: {patient['name']}
Age: {patient.get('age', 'Unknown')} years
Weight: {patient.get('weight_lbs', 'Unknown')} lbs
Days Post-Stroke: {days_post_stroke}
Medications: {patient.get('medications', 'None listed')}
Allergies: {patient.get('allergies', 'None known')}
Medical History: {patient.get('medical_history', 'None recorded')}

LATEST VITAL SIGNS:
"""

    if latest_vitals:
        for vital in latest_vitals:
            change_str = ""
            if vital.get('change_from_previous') is not None:
                change = vital['change_from_previous']
                change_str = f" (Change: {'+' if change > 0 else ''}{change:.1f})"
            context += f"- {vital['reading_type'].replace('_', ' ').title()}: {vital['value']} {vital['unit']}{change_str}\n"
    else:
        context += "- No vitals recorded yet.\n"

    if memories:
        context += "\nKEY FINDINGS FROM PREVIOUS CONSULTATIONS:\n"
        for mem in memories[:10]:
            context += f"- [{mem['doctor_type'].upper()}] {mem['content']}\n"

    if files:
        # File text is appended once, in run_consultation (audit bug 5).
        context += f"\nMEDICAL RECORDS ON FILE: {len(files)} documents\n"
        for f in files[:5]:
            context += f"- {f['filename']} (uploaded {f['uploaded_at'].strftime('%Y-%m-%d') if f['uploaded_at'] else 'unknown'})\n"

    return context

def extract_medical_data_from_image(image_path, image_type="general"):
    """Use GPT-4 Vision to extract medical data"""
    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')

        prompts = {
            "cgm": "Extract glucose readings from this CGM screenshot. Return JSON array: [{\"timestamp\": \"...\", \"glucose_mg_dl\": 142, \"notes\": \"...\"}]",
            "blood_pressure": "Extract BP readings. Return JSON array: [{\"timestamp\": \"...\", \"systolic\": 135, \"diastolic\": 82, \"pulse\": 72}]",
            "lab_results": "Extract all lab values. Return JSON array: [{\"test_name\": \"...\", \"value\": 138, \"unit\": \"...\", \"reference_range\": \"...\"}]",
            "xray": "Analyze this X-ray image. Describe findings, abnormalities, and recommendations for an elderly patient. Focus on age-related vs pathological changes.",
            "general": "Extract any medical data visible. Return JSON array with type, value, unit, timestamp fields."
        }

        client = openai.OpenAI(api_key=get_api_key(), base_url=get_base_url())
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompts.get(image_type, prompts["general"])},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                ]
            }],
            max_tokens=1500
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = re.sub(r'^```\w*\n?', '', content)
            content = re.sub(r'\n?```$', '', content)

        if image_type in ["xray"]:
            return {"analysis": content}, None

        try:
            data = json.loads(content)
            return data, None
        except:
            return {"raw_response": content}, None

    except Exception as e:
        return None, str(e)

def process_dicom_file(file_data, filename):
    """Process DICOM file and extract image + metadata"""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dcm') as tmp:
            tmp.write(file_data)
            tmp_path = tmp.name

        ds = pydicom.dcmread(tmp_path)

        metadata = {
            "patient_name": str(ds.PatientName) if hasattr(ds, 'PatientName') else "Unknown",
            "study_date": str(ds.StudyDate) if hasattr(ds, 'StudyDate') else "Unknown",
            "modality": str(ds.Modality) if hasattr(ds, 'Modality') else "Unknown",
            "body_part": str(ds.BodyPartExamined) if hasattr(ds, 'BodyPartExamined') else "Unknown",
            "study_description": str(ds.StudyDescription) if hasattr(ds, 'StudyDescription') else "Unknown",
            "series_description": str(ds.SeriesDescription) if hasattr(ds, 'SeriesDescription') else "Unknown",
        }

        image = None
        if hasattr(ds, 'pixel_array'):
            arr = ds.pixel_array
            arr = apply_voi_lut(arr, ds)
            if arr.dtype != np.uint8:
                arr = ((arr - arr.min()) / (arr.max() - arr.min()) * 255).astype(np.uint8)
            image = Image.fromarray(arr)

        os.unlink(tmp_path)
        return image, metadata, None

    except Exception as e:
        return None, None, str(e)

def analyze_dicom_with_ai(image, metadata, query=""):
    """Analyze DICOM image with GPT-4 Vision"""
    try:
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        image_data = base64.b64encode(buf.getvalue()).decode('utf-8')

        prompt = f"""You are an expert neuroradiologist specializing in elderly care, recognized as one of the best in the US.

DICOM Metadata:
- Modality: {metadata.get('modality', 'Unknown')}
- Body Part: {metadata.get('body_part', 'Unknown')}
- Study: {metadata.get('study_description', 'Unknown')}
- Series: {metadata.get('series_description', 'Unknown')}

Analyze this medical image. Consider:
1. Normal age-related changes vs pathological findings
2. Any acute abnormalities requiring immediate attention
3. Chronic conditions or degenerative changes
4. Recommendations for further imaging or follow-up

{f"Specific question: {query}" if query else ""}

Provide a detailed radiological report."""

        client = openai.OpenAI(api_key=get_api_key(), base_url=get_base_url())
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
                ]
            }],
            max_tokens=2000
        )

        return response.choices[0].message.content, None
    except Exception as e:
        return None, str(e)

def run_consultation(patient_id, query, agents, addressed_to, user_id):
    """Run medical board consultation"""
    patient_context = get_patient_context(patient_id, user_id)

    files = db.get_patient_files(patient_id, user_id)
    file_context = ""
    for f in files:
        if f.get('extracted_text'):
            file_context += f"\n--- {f['filename']} ---\n{f['extracted_text'][:1000]}\n"

    full_context = f"{patient_context}\n\nMEDICAL RECORDS:\n{file_context}\n\nQUERY: {query}"

    if addressed_to and addressed_to in agents and addressed_to != 'coordinator':
        task = Task(
            description=f"As the {addressed_to}, respond to this query:\n{full_context}",
            agent=agents[addressed_to],
            expected_output="Specialist response to the query"
        )
        crew = Crew(agents=[agents[addressed_to]], tasks=[task], process=Process.sequential, verbose=True)
        result = crew.kickoff()

        return str(result), addressed_to

    tasks = []
    active_agents = []

    for agent_type in db.SPECIALIST_ORDER:
        if agent_type in agents:
            task = Task(
                description=f"Review patient from {agent_type} perspective:\n{full_context}",
                agent=agents[agent_type],
                expected_output=f"{agent_type.title()} assessment"
            )
            tasks.append(task)
            active_agents.append(agents[agent_type])

    if 'coordinator' in agents:
        coord_task = Task(
            description=f"""Synthesize all specialist inputs into unified recommendations.
Apply governance: Neurologist > Cardiologist > Nephrologist > Endocrinologist > Radiologist.

Format:
## MEDICAL BOARD CONSENSUS
[Summary]

## ACTION ITEMS
- RED (Urgent): [items]
- AMBER (Monitor): [items]
- GREEN (Continue): [items]

## SPECIALIST INSIGHTS
[Key points from each]

{full_context}""",
            agent=agents['coordinator'],
            expected_output="Unified consensus with action items",
            context=tasks
        )
        tasks.append(coord_task)
        active_agents.append(agents['coordinator'])

    crew = Crew(agents=active_agents, tasks=tasks, process=Process.sequential, verbose=True)
    result = crew.kickoff()

    # No "Consulted on" memory rows per specialist (audit bug 6). Real findings memory is item 9.
    return str(result), 'coordinator'

def render_chat_interface_with_conversations(patient_id, agents, user_id):
    """Render chat interface with conversation support"""
    st.markdown("""
    <style>
    .stChatMessage {
        padding: 1rem;
        border-radius: 10px;
        margin-bottom: 0.5rem;
    }
    </style>
    """, unsafe_allow_html=True)

    conversation_id = st.session_state.get('current_conversation_id')

    if not conversation_id:
        conversations = db.get_user_conversations(user_id, patient_id)
        if conversations:
            st.session_state.current_conversation_id = conversations[0]['id']
            conversation_id = conversations[0]['id']
        else:
            st.info("Start a new conversation using the '+ New Chat' button in the sidebar.")
            if st.button("Start First Conversation", type="primary"):
                patient = db.get_patient(patient_id, user_id)
                try:
                    conv_id = db.create_conversation(user_id, patient_id, f"Consultation for {patient['name']}")
                    st.session_state.current_conversation_id = conv_id
                    st.rerun()
                except PermissionError:
                    show_access_denied()
            return

    conv = db.get_conversation(conversation_id, user_id)
    if conv:
        st.caption(f"📋 {conv.get('title', 'Consultation')}")

    chat_container = st.container()

    with chat_container:
        history = db.get_conversation_history(conversation_id, user_id, limit=50)

        if not history:
            st.info("No messages yet. Ask the medical board a question to start the consultation.")

        for msg in history:
            if msg['role'] == 'user':
                with st.chat_message("user"):
                    if msg.get('addressed_to'):
                        st.caption(f"To: {msg['addressed_to'].title()}")
                    st.write(msg['content'])
            else:
                with st.chat_message("assistant"):
                    if msg.get('responding_doctor'):
                        doctor = msg['responding_doctor']
                        icons = {
                            'neurologist': '🧠', 'cardiologist': '❤️', 'nephrologist': '🫘',
                            'endocrinologist': '💉', 'radiologist': '📷', 'coordinator': '👨‍⚕️'
                        }
                        st.caption(f"{icons.get(doctor, '👨‍⚕️')} {doctor.title()}")
                    st.write(msg['content'])

    user_input = st.chat_input("Ask the medical board... (mention a specific doctor to address them directly)")

    if user_input:
        addressed_to = parse_addressed_doctor(user_input, agent_types=agents.keys())
        try:
            db.save_chat_message_to_conversation(patient_id, conversation_id, user_id, 'user', user_input,
                                                 addressed_to=addressed_to)

            if len(history) == 0:
                title = user_input[:50] + "..." if len(user_input) > 50 else user_input
                db.update_conversation_title(conversation_id, user_id, title)
        except PermissionError:
            show_access_denied()
            return

        with st.spinner("Medical board consulting..."):
            response, responding_doctor = run_consultation(patient_id, user_input, agents, addressed_to, user_id)

        try:
            db.save_chat_message_to_conversation(patient_id, conversation_id, user_id, 'assistant', response,
                                                 responding_doctor=responding_doctor)
        except PermissionError:
            show_access_denied()
            return
        st.rerun()

def render_patient_info_tab(patient_id, user_id):
    """Render patient info editing tab"""
    patient = db.get_patient(patient_id, user_id)
    if not patient:
        st.warning("No patient selected")
        return

    st.subheader(f"Patient: {patient['name']}")

    col1, col2 = st.columns(2)

    with col1:
        name = st.text_input("Name", value=patient.get('name', ''))
        age = st.number_input("Age", value=patient.get('age') or 65, min_value=0, max_value=150)
        weight = st.number_input("Weight (lbs)", value=float(patient.get('weight_lbs') or 0))
        stroke_date = st.date_input("Stroke Date", value=patient.get('stroke_date'))

    with col2:
        medications = st.text_area("Medications", value=patient.get('medications') or '')
        allergies = st.text_area("Allergies", value=patient.get('allergies') or '')
        medical_history = st.text_area("Medical History", value=patient.get('medical_history') or '')

    if st.button("Save Patient Info"):
        try:
            db.update_patient(patient_id, user_id,
                name=name, age=age, weight_lbs=weight,
                medications=medications, allergies=allergies,
                medical_history=medical_history, stroke_date=stroke_date
            )
            st.success("Patient information saved!")
        except PermissionError:
            show_access_denied()

    st.divider()

    if st.button("Delete Patient", type="secondary"):
        if st.session_state.get('confirm_delete') == patient_id:
            try:
                db.delete_patient(patient_id, user_id)
                st.session_state.current_patient_id = None
                st.session_state.confirm_delete = None
                st.rerun()
            except PermissionError:
                show_access_denied()
        else:
            st.session_state.confirm_delete = patient_id
            st.warning("Click again to confirm deletion")

def render_files_tab(patient_id, user_id):
    """Render files management tab"""
    st.subheader("Medical Files")

    uploaded = st.file_uploader(
        "Upload medical files",
        type=['pdf', 'txt', 'jpg', 'jpeg', 'png', 'dcm'],
        accept_multiple_files=True
    )

    file_type = st.selectbox("File Type", ["general", "cgm", "blood_pressure", "lab_results", "xray", "dicom"])

    if uploaded and st.button("Process & Save Files"):
        for file in uploaded:
            file_data = file.read()
            extracted_text = ""
            extracted_values = None

            if file.name.endswith('.dcm') or file_type == 'dicom':
                image, metadata, error = process_dicom_file(file_data, file.name)
                if error:
                    st.error(f"Error processing {file.name}: {error}")
                    continue
                if image:
                    analysis, _ = analyze_dicom_with_ai(image, metadata)
                    extracted_text = f"DICOM Metadata: {json.dumps(metadata)}\n\nAnalysis: {analysis}"
                    extracted_values = metadata
            elif file.type and file.type.startswith('image'):
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file.name.split(".")[-1]}') as tmp:
                    tmp.write(file_data)
                    data, error = extract_medical_data_from_image(tmp.name, file_type)
                    os.unlink(tmp.name)
                    if data:
                        extracted_text = json.dumps(data, indent=2)
                        extracted_values = data

            try:
                file_id = db.save_patient_file(patient_id, user_id, file.name, file_type, file_data,
                                               extracted_text, extracted_values)
                # Push extracted readings into vitals_history so charts and context see them (audit bug 1).
                if extracted_values is not None and file_type in ("cgm", "blood_pressure", "lab_results", "general"):
                    rows = normalize_extracted_readings(file_type, extracted_values)
                    n = db.save_extracted_readings(patient_id, user_id, file_id, file.name, rows)
                    if n:
                        st.info(f"{n} readings added to vitals history")
                st.success(f"Saved: {file.name}")
            except PermissionError:
                show_access_denied()

    st.divider()
    st.subheader("Stored Files")

    files = db.get_patient_files(patient_id, user_id)
    for f in files:
        with st.expander(f"{f['filename']} - {f['file_type']} ({f['uploaded_at']})"):
            if f.get('extracted_text'):
                st.text_area("Extracted Data", value=f['extracted_text'], height=150, disabled=True)

            if f['file_type'] == 'dicom' or f['filename'].endswith('.dcm'):
                if st.button(f"View DICOM: {f['id']}", key=f"view_{f['id']}"):
                    file_record = db.get_file_data(f['id'], user_id)
                    if file_record:
                        image, metadata, _ = process_dicom_file(bytes(file_record['file_data']), f['filename'])
                        if image:
                            st.image(image, caption=f"DICOM: {f['filename']}")
                            st.json(metadata)

def render_agent_settings():
    """Render agent customization settings"""
    st.subheader("Customize Doctor Agents")

    agents = db.get_doctor_agents(active_only=False)

    for agent in agents:
        with st.expander(f"{agent['display_name']} ({agent['agent_type']})"):
            is_active = st.toggle("Active", value=bool(agent['is_active']), key=f"active_{agent['agent_type']}")
            role = st.text_input("Role", value=agent['role'], key=f"role_{agent['agent_type']}")
            goal = st.text_area("Goal", value=agent['goal'], key=f"goal_{agent['agent_type']}", height=80)
            backstory = st.text_area("Backstory", value=agent['backstory'], key=f"backstory_{agent['agent_type']}", height=150)

            if st.button(f"Save {agent['display_name']}", key=f"save_{agent['agent_type']}"):
                db.update_doctor_agent(agent['agent_type'], role=role, goal=goal, backstory=backstory,
                                       is_active=is_active)
                st.success(f"Updated {agent['display_name']}")

def render_vitals_tab(patient_id, user_id):
    """Render vitals entry and history"""
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Enter Vitals")
        vital_type = st.selectbox("Vital Type", [
            "blood_pressure_systolic", "blood_pressure_diastolic",
            "heart_rate", "glucose", "temperature", "oxygen_saturation",
            "weight", "creatinine", "hemoglobin"
        ])
        value = st.number_input("Value", step=0.1)
        unit = st.text_input("Unit", value="mmHg" if "pressure" in vital_type else "")
        notes = st.text_input("Notes (optional)")

        if st.button("Save Vital"):
            try:
                db.save_vital_reading(patient_id, user_id, vital_type, value, unit, "Manual Entry", notes)
                st.success("Vital saved!")
                st.rerun()
            except PermissionError:
                show_access_denied()

    with col2:
        st.subheader("Latest Vitals")
        vitals = db.get_latest_vitals(patient_id, user_id)
        if vitals:
            for v in vitals:
                change_str = ""
                if v.get('change_from_previous'):
                    change = v['change_from_previous']
                    color = "green" if change < 0 else "red"
                    change_str = f" (:{color}[{'+' if change > 0 else ''}{change:.1f}])"
                st.metric(v['reading_type'].replace('_', ' ').title(), f"{v['value']} {v['unit']}")
        else:
            st.info("No vitals recorded")

    st.divider()
    st.subheader("Vitals History")

    all_vitals = db.get_vitals_history(patient_id, user_id)
    if all_vitals:
        df = pd.DataFrame(all_vitals)
        # recorded_at is when the reading was taken; NULL means the document gave no time.
        df['recorded_at'] = df['recorded_at'].apply(lambda v: "unknown" if pd.isna(v) else v)
        df = df[['reading_type', 'value', 'unit', 'recorded_at', 'timestamp', 'change_from_previous', 'source']]
        df = df.rename(columns={'recorded_at': 'Recorded', 'timestamp': 'Ingested'})
        st.dataframe(df)

def inject_pwa_tags():
    """Inject PWA meta tags for iOS Add to Home Screen support"""
    pwa_html = """
    <link rel="manifest" href="/static/manifest.json">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="MedBoard">
    <meta name="theme-color" content="#667eea">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    """
    st.markdown(pwa_html, unsafe_allow_html=True)

def main():
    st.set_page_config(page_title="Medical Board AI", layout="wide", page_icon="🏥")
    inject_pwa_tags()

    if not os.environ.get("DATABASE_URL"):
        st.error("Database not configured. Please set up the PostgreSQL database.")
        return

    try:
        db.init_database()
    except Exception as e:
        st.error(f"Database initialization failed: {e}")
        return

    api_key = get_api_key()
    if not api_key:
        st.error("OpenAI API not configured. Please set up the OpenAI integration.")
        return

    if 'user_id' not in st.session_state:
        render_login_page()
        return

    user_id = st.session_state.user_id
    user = st.session_state.get('user', {})

    with st.sidebar:
        st.markdown(f"### 👤 {user.get('first_name', 'User')}")
        if st.button("Sign Out", use_container_width=True):
            del st.session_state.user_id
            if 'user' in st.session_state:
                del st.session_state.user
            if 'current_patient_id' in st.session_state:
                del st.session_state.current_patient_id
            if 'current_conversation_id' in st.session_state:
                del st.session_state.current_conversation_id
            st.rerun()

        st.divider()

        st.markdown("### 👥 Patients")
        patients = db.get_all_patients_for_user_or_orphans(user_id)

        patient_options = {p['id']: p['name'] for p in patients}
        patient_options[0] = "➕ Add New Patient"

        current_patient = st.session_state.get('current_patient_id')
        if current_patient and current_patient not in patient_options:
            current_patient = None

        selected = st.selectbox(
            "Select Patient",
            options=list(patient_options.keys()),
            format_func=lambda x: patient_options.get(x, "Select"),
            index=list(patient_options.keys()).index(current_patient) if current_patient else 0,
            key="patient_selector"
        )

        if selected == 0:
            with st.expander("New Patient", expanded=True):
                new_name = st.text_input("Patient Name", key="new_patient_name")
                new_age = st.number_input("Age", min_value=0, max_value=150, value=65, key="new_patient_age")
                if st.button("Create Patient", use_container_width=True):
                    if new_name:
                        new_id = db.create_patient_for_user(user_id, new_name, age=new_age)
                        st.session_state.current_patient_id = new_id
                        st.rerun()
        elif selected:
            st.session_state.current_patient_id = selected

        st.divider()
        render_chat_sidebar(user_id)

        st.divider()
        st.caption("AI-generated consultation for educational purposes only.")

    patient_id = st.session_state.get('current_patient_id')

    st.title("🏥 Medical Board Consultation System")
    st.caption("Multi-specialist AI consultation with patient management")

    if not patient_id:
        st.info("Please create or select a patient from the sidebar to begin.")
        return

    if not db.verify_patient_access(patient_id, user_id):
        st.error("You don't have access to this patient.")
        st.session_state.current_patient_id = None
        return

    agents = create_agents_from_db(api_key)

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "💬 Chat", "📁 Files & DICOM", "📊 Vitals",
        "👤 Patient Info", "⚙️ Agent Settings", "📷 Radiology"
    ])

    with tab1:
        render_chat_interface_with_conversations(patient_id, agents, user_id)

    with tab2:
        render_files_tab(patient_id, user_id)

    with tab3:
        render_vitals_tab(patient_id, user_id)

    with tab4:
        render_patient_info_tab(patient_id, user_id)

    with tab5:
        render_agent_settings()

    with tab6:
        st.subheader("Radiology - DICOM Viewer & Analysis")

        dicom_file = st.file_uploader("Upload DICOM file", type=['dcm'], key="dicom_upload")

        if dicom_file:
            file_data = dicom_file.read()
            image, metadata, error = process_dicom_file(file_data, dicom_file.name)

            if error:
                st.error(f"Error: {error}")
            elif image:
                col1, col2 = st.columns([2, 1])

                with col1:
                    st.image(image, caption="DICOM Image", use_container_width=True)

                with col2:
                    st.subheader("DICOM Metadata")
                    for key, val in metadata.items():
                        st.text(f"{key}: {val}")

                query = st.text_input("Ask the radiologist about this image:")

                if st.button("Analyze Image"):
                    with st.spinner("Radiologist analyzing..."):
                        analysis, error = analyze_dicom_with_ai(image, metadata, query)
                        if analysis:
                            st.markdown("### Radiologist Report")
                            st.write(analysis)

                            try:
                                db.save_patient_file(patient_id, user_id, dicom_file.name, 'dicom', file_data,
                                                     analysis, metadata)
                                db.save_doctor_memory(patient_id, user_id, 'radiologist', 'imaging_finding',
                                                      analysis[:500], importance=7)
                            except PermissionError:
                                show_access_denied()
                        else:
                            st.error(f"Analysis error: {error}")

if __name__ == "__main__":
    main()
