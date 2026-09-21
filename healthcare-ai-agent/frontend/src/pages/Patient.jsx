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
    r.onresult = (e) => onFinal(e.results[e.results.length - 1][0].transcript)
    ref.current = r

    return () => {
      if (ref.current) ref.current.stop()
    }
  }, [onFinal])
  return ref
}

function speak(text) {
  if (!window.speechSynthesis) return
  window.speechSynthesis.cancel()
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text))
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
  const [isAuthenticated, setIsAuthenticated] = useState(!!localStorage.getItem('token'))

  const chatEndRef = useRef(null)

  // Auto-scroll chat to latest message
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, busy])

  // Check auth and refresh patient data
  const refresh = async () => {
    const token = localStorage.getItem('token')
    if (!token) {
      setIsAuthenticated(false)
      return
    }
    setIsAuthenticated(true)

    try {
      const [appts, qsets] = await Promise.all([
      api.myAppointments(),
      api.myQuestionnaires(),
      ])

      setAppointments(appts || [])

      const uniqueQuestionnaires = Array.from(
      new Map(
      (qsets || []).map((q) => [q.name, q])
      ).values()
    )

    setQuestionnaires(uniqueQuestionnaires)
    } catch (e) {
      if (e.message?.includes('401') || e.message?.includes('Unauthorized')) {
        setIsAuthenticated(false)
      }
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const send = async (text) => {
    if (!text.trim() || busy) return

    if (!localStorage.getItem('token')) {
      setError('Your session has expired. Please log in again.')
      setIsAuthenticated(false)
      return
    }

    setMessages((m) => [...m, { role: 'user', content: text }])
    setInput('')
    setBusy(true)
    setError('')

    try {
      const r = await api.chat(text, conversationId)
      setConversationId(r.conversation_id)
      setMessages((m) => [...m, { role: 'assistant', content: r.reply, used: r.capabilities_used }])
      speak(r.reply)
      refresh()
    } catch (e) {
      if (e.message?.includes('401') || e.message?.includes('Unauthorized')) {
        setError('Session expired. Please log in again.')
        setIsAuthenticated(false)
      } else {
        setError(e.message || 'The assistant is unavailable. Check the backend logs.')
      }
    }
    setBusy(false)
  }

  const recognizer = useSpeech(send)

  const toggleMic = () => {
    if (!recognizer.current) return setError('This browser has no speech recognition. Type instead.')
    if (listening) {
      recognizer.current.stop()
      setListening(false)
    } else {
      recognizer.current.start()
      setListening(true)
    }
  }

  // Render Login Prompt if Not Authenticated
  if (!isAuthenticated) {
    return (
      <div className="card" style={{ maxWidth: '450px', margin: '3rem auto', textAlign: 'center', padding: '2rem' }}>
        <h2>Authentication Required</h2>
        <p className="hint">Please log in to talk with the AI assistant and view your appointments.</p>
        <button
          onClick={() => (window.location.href = '/login')}
          style={{ marginTop: '1.5rem', width: '100%' }}
        >
          Go to Login
        </button>
      </div>
    )
  }

  return (
    <div className="grid">
      <section className="card">
        <h2>Talk to the assistant</h2>
        <div className="chat">
          {messages.length === 0 && (
            <p className="hint">Try: “I need to see a doctor for my shoulder pain this week.”</p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`bubble ${m.role}`}>
              <p>{m.content}</p>
              {m.used?.length > 0 && <small>{m.used.join(' → ')}</small>}
            </div>
          ))}
          {busy && <div className="bubble assistant"><p>…</p></div>}
          <div ref={chatEndRef} />
        </div>
        {error && <p className="error">{error}</p>}
        <div className="row">
          <input
            value={input}
            placeholder="Type or press the mic"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send(input)}
          />
          <button onClick={() => send(input)} disabled={busy}>Send</button>
          <button onClick={toggleMic} className={listening ? 'live' : ''}>
            {listening ? 'Stop' : 'Speak'}
          </button>
        </div>
      </section>

      <section className="card">
        <h2>Your appointments</h2>
        {appointments.length === 0 && <p className="hint">Nothing booked yet.</p>}
        <ul className="list">
          {appointments.map((a) => (
            <li key={a.id}>
              <strong>{new Date(a.when).toLocaleString()}</strong>
              <span>{a.doctor} · {a.hospital}</span>
              <span className={`tag ${a.status}`}>{a.status}</span>
              {a.status !== 'CANCELLED' && (
                <button onClick={() => api.cancel(a.id).then(refresh)}>Cancel</button>
              )}
            </li>
          ))}
        </ul>

        <h2>Pre-visit questions</h2>
        {questionnaires.length === 0 && <p className="hint">Assigned after you book.</p>}
        {questionnaires.map((q) => (
          <div key={q.response_id} className="qset">
            <p>{q.name} — {q.status}</p>
            {q.questions?.map((item) => (
              <div key={item.key} className="qrow">
                <label>{item.text}</label>
                <input
                  defaultValue={q.answers?.[item.key] || ''}
                  onBlur={(e) =>
                    e.target.value &&
                    api.answer(q.response_id, { [item.key]: e.target.value }).then(refresh)
                  }
                />
              </div>
            ))}
          </div>
        ))}
      </section>
    </div>
  )
}