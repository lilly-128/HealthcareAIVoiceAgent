import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

// Browser speech in, browser speech out.
function useSpeech(onFinal) {
  const ref = useRef(null)

  useEffect(() => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition

    if (!SR) return

    const r = new SR()

    r.lang = 'en-IN'
    r.interimResults = false

    r.onresult = (e) => {
      const text =
        e.results[e.results.length - 1][0].transcript

      onFinal(text)
    }

    r.onend = () => {
      // microphone stopped
    }

    ref.current = r

    return () => {
      if (ref.current) {
        ref.current.stop()
      }
    }
  }, [onFinal])

  return ref
}

function speak(text) {
  if (!window.speechSynthesis || !text) return

  window.speechSynthesis.cancel()

  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'en-IN'

  window.speechSynthesis.speak(utterance)
}

export default function Patient() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState(null)
  const [busy, setBusy] = useState(false)

  const [appointments, setAppointments] = useState([])
  const [questionnaires, setQuestionnaires] = useState([])

  const [listening, setListening] = useState(false)
  const [error, setError] = useState('')

  const [isAuthenticated, setIsAuthenticated] = useState(
    !!localStorage.getItem('token')
  )

  const chatEndRef = useRef(null)

  // --------------------------------------------------
  // AUTO SCROLL
  // --------------------------------------------------

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({
      behavior: 'smooth',
    })
  }, [messages, busy])

  // --------------------------------------------------
  // REFRESH PATIENT DATA
  // --------------------------------------------------

  const refresh = async () => {
    const token = localStorage.getItem('token')

    if (!token) {
      setIsAuthenticated(false)
      return
    }

    setIsAuthenticated(true)

    try {
      const [apptsResponse, qsetsResponse] = await Promise.all([
        api.myAppointments(),
        api.myQuestionnaires(),
      ])

      // ----------------------------------------------
      // HANDLE APPOINTMENT RESPONSE
      // ----------------------------------------------

      let appts = []

      if (Array.isArray(apptsResponse)) {
        appts = apptsResponse
      } else if (Array.isArray(apptsResponse?.appointments)) {
        appts = apptsResponse.appointments
      } else if (Array.isArray(apptsResponse?.data)) {
        appts = apptsResponse.data
      }

      console.log('APPOINTMENTS FROM API:', appts)

      setAppointments(appts)

      // ----------------------------------------------
      // HANDLE QUESTIONNAIRE RESPONSE
      // ----------------------------------------------

      let qsets = []

      if (Array.isArray(qsetsResponse)) {
        qsets = qsetsResponse
      } else if (Array.isArray(qsetsResponse?.questionnaires)) {
        qsets = qsetsResponse.questionnaires
      } else if (Array.isArray(qsetsResponse?.data)) {
        qsets = qsetsResponse.data
      }

      const uniqueQuestionnaires = Array.from(
        new Map(
          qsets.map((q) => [q.name, q])
        ).values()
      )

      setQuestionnaires(uniqueQuestionnaires)

    } catch (e) {
      console.error('REFRESH ERROR:', e)

      if (
        e.message?.includes('401') ||
        e.message?.includes('Unauthorized')
      ) {
        setIsAuthenticated(false)
      } else {
        setError(
          e.message || 'Unable to load appointments.'
        )
      }
    }
  }

  // --------------------------------------------------
  // INITIAL LOAD
  // --------------------------------------------------

  useEffect(() => {
    refresh()
  }, [])

  // --------------------------------------------------
  // SEND MESSAGE
  // --------------------------------------------------

  const send = async (text) => {
    if (!text.trim() || busy) return

    const token = localStorage.getItem('token')

    if (!token) {
      setError(
        'Your session has expired. Please log in again.'
      )

      setIsAuthenticated(false)
      return
    }

    setMessages((m) => [
      ...m,
      {
        role: 'user',
        content: text,
      },
    ])

    setInput('')
    setBusy(true)
    setError('')

    try {
      console.log('SENDING AI MESSAGE:', text)

      const r = await api.chat(
        text,
        conversationId
      )

      console.log('AI RESPONSE:', r)

      if (r?.conversation_id) {
        setConversationId(r.conversation_id)
      }

      const reply =
        r?.reply ||
        r?.response ||
        'The assistant did not return a response.'

      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content: reply,
          used: r?.capabilities_used || [],
        },
      ])

      speak(reply)

      // IMPORTANT:
      // Wait for appointments to refresh AFTER booking
      await refresh()

    } catch (e) {
      console.error('AI CHAT ERROR:', e)

      if (
        e.message?.includes('401') ||
        e.message?.includes('Unauthorized')
      ) {
        setError(
          'Session expired. Please log in again.'
        )

        setIsAuthenticated(false)
      } else {
        setError(
          e.message ||
          'The assistant is unavailable. Check the backend logs.'
        )
      }
    } finally {
      setBusy(false)
    }
  }

  // --------------------------------------------------
  // SPEECH RECOGNITION
  // --------------------------------------------------

  const recognizer = useSpeech(send)

  const toggleMic = () => {
    if (!recognizer.current) {
      setError(
        'This browser has no speech recognition. Type instead.'
      )

      return
    }

    if (listening) {
      recognizer.current.stop()
      setListening(false)
    } else {
      try {
        recognizer.current.start()
        setListening(true)
      } catch (e) {
        console.error('MIC ERROR:', e)
      }
    }
  }

  // --------------------------------------------------
  // CANCEL APPOINTMENT
  // --------------------------------------------------

  const cancelAppointment = async (id) => {
    try {
      setError('')

      await api.cancel(id)

      // Immediately reload appointments
      await refresh()

    } catch (e) {
      console.error('CANCEL ERROR:', e)

      setError(
        e.message || 'Unable to cancel appointment.'
      )
    }
  }

  // --------------------------------------------------
  // NOT AUTHENTICATED
  // --------------------------------------------------

  if (!isAuthenticated) {
    return (
      <div
        className="card"
        style={{
          maxWidth: '450px',
          margin: '3rem auto',
          textAlign: 'center',
          padding: '2rem',
        }}
      >
        <h2>Authentication Required</h2>

        <p className="hint">
          Please log in to talk with the AI assistant
          and view your appointments.
        </p>

        <button
          onClick={() =>
            (window.location.href = '/login')
          }
          style={{
            marginTop: '1.5rem',
            width: '100%',
          }}
        >
          Go to Login
        </button>
      </div>
    )
  }

  // --------------------------------------------------
  // UI
  // --------------------------------------------------

  return (
    <div className="grid">

      {/* ==========================================
          AI ASSISTANT
      ========================================== */}

      <section className="card">

        <h2>Talk to the assistant</h2>

        <div className="chat">

          {messages.length === 0 && (
            <p className="hint">
              Try: “I need to see a doctor for my shoulder pain this week.”
            </p>
          )}

          {messages.map((m, i) => (
            <div
              key={i}
              className={`bubble ${m.role}`}
            >
              <p>{m.content}</p>

              {m.used?.length > 0 && (
                <small>
                  {m.used.join(' → ')}
                </small>
              )}
            </div>
          ))}

          {busy && (
            <div className="bubble assistant">
              <p>…</p>
            </div>
          )}

          <div ref={chatEndRef} />

        </div>

        {error && (
          <p className="error">
            {error}
          </p>
        )}

        <div className="row">

          <input
            value={input}
            placeholder="Type or press the mic"
            onChange={(e) =>
              setInput(e.target.value)
            }
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                send(input)
              }
            }}
          />

          <button
            onClick={() => send(input)}
            disabled={busy}
          >
            Send
          </button>

          <button
            onClick={toggleMic}
            className={
              listening ? 'live' : ''
            }
          >
            {listening ? 'Stop' : 'Speak'}
          </button>

        </div>

      </section>

      {/* ==========================================
          APPOINTMENTS
      ========================================== */}

      <section className="card">

        <h2>Your appointments</h2>

        {appointments.length === 0 && (
          <p className="hint">
            Nothing booked yet.
          </p>
        )}

        <ul className="list">

          {appointments.map((a, index) => {

            const appointmentId =
              a.id ||
              a.appointment_id

            const appointmentDate =
              a.when ||
              a.start_at ||
              a.start ||
              a.appointment_date

            const doctor =
              a.doctor ||
              a.doctor_name ||
              'Doctor'

            const hospital =
              a.hospital ||
              a.hospital_name ||
              'Hospital'

            const status =
              a.status ||
              'PENDING'

            return (
              <li
                key={
                  appointmentId ||
                  index
                }
              >

                <strong>
                  {appointmentDate
                    ? new Date(
                        appointmentDate
                      ).toLocaleString('en-IN')
                    : 'Date not available'}
                </strong>

                <span>
                  {doctor} · {hospital}
                </span>

                <span
                  className={`tag ${String(
                    status
                  ).toUpperCase()}`}
                >
                  {String(status).toUpperCase()}
                </span>

                {String(status).toUpperCase() !==
                  'CANCELLED' && (
                  <button
                    onClick={() =>
                      cancelAppointment(
                        appointmentId
                      )
                    }
                  >
                    Cancel
                  </button>
                )}

              </li>
            )
          })}

        </ul>

        {/* ==========================================
            PRE-VISIT QUESTIONS
        ========================================== */}

        <h2>Pre-visit questions</h2>

        {questionnaires.length === 0 && (
          <p className="hint">
            Assigned after you book.
          </p>
        )}

        {questionnaires.map((q) => (

          <div
            key={q.response_id}
            className="qset"
          >

            <p>
              {q.name} — {q.status}
            </p>

            {q.questions?.map((item) => (

              <div
                key={item.key}
                className="qrow"
              >

                <label>
                  {item.text}
                </label>

                <input
                  defaultValue={
                    q.answers?.[item.key] || ''
                  }
                  onBlur={(e) => {

                    if (!e.target.value) return

                    api.answer(
                      q.response_id,
                      {
                        [item.key]:
                          e.target.value,
                      }
                    ).then(refresh)

                  }}
                />

              </div>

            ))}

          </div>

        ))}

      </section>

    </div>
  )
}
