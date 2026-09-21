import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Doctor() {
  const [rows, setRows] = useState([])
  useEffect(() => { api.doctorAppointments().then(setRows).catch(() => {}) }, [])

  const today = new Date().toDateString()
  const isToday = (w) => new Date(w).toDateString() === today

  return (
    <div className="grid">
      <section className="card">
        <h2>Today</h2>
        {rows.filter((r) => isToday(r.when)).length === 0 && <p className="hint">No visits today.</p>}
        <ul className="list">
          {rows.filter((r) => isToday(r.when)).map((r) => (
            <li key={r.id}>
              <strong>{new Date(r.when).toLocaleTimeString()}</strong>
              <span>{r.patient}</span>
              <span className={`tag ${r.status}`}>{r.status}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>Upcoming and pre-visit answers</h2>
        {rows.map((r) => (
          <div key={r.id} className="qset">
            <p><strong>{new Date(r.when).toLocaleString()}</strong> — {r.patient}
              <span className={`tag ${r.status}`}>{r.status}</span></p>
            {r.pre_visit.length === 0 && <p className="hint">No questionnaire assigned.</p>}
            {r.pre_visit.map((q, i) => (
              <p key={i} className="qa">{q.text}<br /><em>{q.answer || 'not answered yet'}</em></p>
            ))}
          </div>
        ))}
      </section>
    </div>
  )
}
