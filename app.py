if st.button("Generate 20 New Topics"):
    history = ensure_history()

    try:
        with st.spinner("Asking Gemini for 20 new topics..."):
            topics = gemini_generate_topics(history, n=20)

        if not topics:
            st.error("Gemini returned 0 topics. Try again.")
        else:
            # Persist latest topics so dropdown doesn't become empty on reruns
            st.session_state["topics_20"] = topics
            st.session_state["selected_topic"] = topics[0]
            save_latest_topics(topics)

            # Update history (keep it bounded so prompt doesn't blow up)
            history.extend([t.strip().lower() for t in topics if t.strip()])
            history = list(dict.fromkeys(history))[-200:]  # keep last 200
            save_json(TOPIC_HISTORY_FILE, history)

            st.success("20 topics generated.")

    except Exception as e:
        st.error(f"Topic generation failed: {e}")
        st.info("Open 'Manage app' → 'Logs' in Streamlit Cloud for full details.")
