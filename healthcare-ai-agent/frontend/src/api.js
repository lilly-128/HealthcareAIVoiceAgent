const BASE =
  import.meta.env.VITE_API_URL || 'http://localhost:8000'

export const EHR_URL =
  import.meta.env.VITE_EHR_URL || 'http://localhost:9000'


// ====================================================
// GET TOKEN
// ====================================================

function token() {
  return localStorage.getItem('token')
}


// ====================================================
// COMMON API CALL
// ====================================================

async function call(path, { method = 'GET', body } = {}) {

  const currentToken = token()

  console.log('====================================')
  console.log('API REQUEST:', method, path)
  console.log(
    'TOKEN EXISTS:',
    currentToken ? 'YES' : 'NO'
  )

  const headers = {
    'Content-Type': 'application/json',
  }

  // Add JWT Authorization header
  if (currentToken) {
    headers['Authorization'] = `Bearer ${currentToken}`

    console.log('AUTH HEADER: Bearer token added')
  } else {
    console.error('AUTH HEADER: NO TOKEN FOUND')
  }

  const res = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  })


  // ==================================================
  // READ RESPONSE
  // ==================================================

  let data = null

  try {
    data = await res.json()
  } catch {
    data = null
  }


  // ==================================================
  // 401 UNAUTHORIZED
  // ==================================================

  if (res.status === 401) {

    console.error(
      '401 Unauthorized:',
      data
    )

    throw new Error(
      data?.detail ||
      'Unauthorized. Please login again.'
    )
  }


  // ==================================================
  // 403 FORBIDDEN
  // ==================================================

  if (res.status === 403) {

    console.error(
      '403 Forbidden:',
      data
    )

    throw new Error(
      data?.detail ||
      'You do not have permission to perform this action.'
    )
  }


  // ==================================================
  // OTHER ERRORS
  // ==================================================

  if (!res.ok) {

    console.error(
      `API ERROR ${res.status}:`,
      data
    )

    throw new Error(
      data?.detail ||
      JSON.stringify(data) ||
      res.statusText
    )
  }


  // ==================================================
  // SUCCESS
  // ==================================================

  console.log(
    'API SUCCESS:',
    method,
    path,
    res.status
  )

  console.log('====================================')

  return data
}


// ====================================================
// API
// ====================================================

export const api = {


  // ==================================================
  // AUTH
  // ==================================================

  login: async (email, password) => {
  const data = await call('/auth/login', {
    method: 'POST',
    body: {
      email,
      password,
    },
  })

  console.log('LOGIN RESPONSE:', data)

  if (!data?.access_token) {
    console.error('LOGIN ERROR: Backend did not return access_token')
    throw new Error('Login failed: no access token received')
  }

  localStorage.setItem('token', data.access_token)

  if (data.hospital_id) {
    localStorage.setItem('hospital_id', data.hospital_id)
  }

  console.log('LOGIN SUCCESS')
  console.log('ROLE:', data.role)
  console.log('TOKEN SAVED:', localStorage.getItem('token') ? 'YES' : 'NO')

  return data
},


  // ==================================================
  // HOSPITAL REGISTRATION
  // ==================================================

  registerHospital: async (body) => {

    return await call(
      '/auth/hospitals/register',
      {
        method: 'POST',
        body,
      }
    )
  },


  // ==================================================
  // PATIENT REGISTRATION
  // ==================================================

  registerPatient: async (body) => {

    const data = await call(
      '/auth/patients/register',
      {
        method: 'POST',
        body,
      }
    )


    if (data.access_token) {

      localStorage.setItem(
        'token',
        data.access_token
      )
    }


    return data
  },


  // ==================================================
  // DOCTOR REGISTRATION
  // ==================================================

  registerDoctor: async (body) => {

    console.log(
      'REGISTER DOCTOR BODY:',
      body
    )

    return await call(
      '/doctors/register',
      {
        method: 'POST',
        body,
      }
    )
  },


  // ==================================================
  // DOCTOR LIST / SEARCH
  // ==================================================

  getDoctors: async (
    specialty = '',
    hospitalId = ''
  ) => {

    const params = new URLSearchParams()

    // Add specialty if provided
    if (specialty && specialty.trim()) {
      params.append(
        'specialty',
        specialty.trim()
      )
    }

    // Add hospital ID if provided
    if (hospitalId && hospitalId.trim()) {
      params.append(
        'hospital_id',
        hospitalId.trim()
      )
    }

    const query =
      params.toString()
        ? `?${params.toString()}`
        : ''

    console.log(
      'SEARCHING DOCTORS:',
      `/doctors${query}`
    )

    return await call(
      `/doctors${query}`
    )
  },


  // ==================================================
  // PATIENT / AI AGENT
  // ==================================================

  chat: (
    message,
    conversation_id = null
  ) => {

    return call('/ai/chat', {
      method: 'POST',

      body: {
        message,
        conversation_id,
        channel: 'WEB_VOICE',
      },
    })
  },


  // ==================================================
  // PATIENT APPOINTMENTS
  // ==================================================

  myAppointments: () =>
    call('/appointments'),


  cancel: (id) =>
    call(
      `/appointments/${id}/cancel`,
      {
        method: 'POST',
      }
    ),


  // ==================================================
  // QUESTIONNAIRES
  // ==================================================

  myQuestionnaires: () =>
    call('/questionnaires/mine'),


  answer: (id, answers) =>
    call(
      `/questionnaires/${id}`,
      {
        method: 'POST',

        body: {
          answers,
        },
      }
    ),


  // ==================================================
  // NOTIFICATIONS
  // ==================================================

  myNotifications: () =>
    call('/notifications/mine'),


  // ==================================================
  // DOCTOR
  // ==================================================

  doctorAppointments: () =>
    call('/doctor/appointments'),


  // ==================================================
  // HOSPITAL
  // ==================================================

  hospitals: () =>
    call('/hospitals'),


  hospital: (hospitalId) =>
    call(
      `/hospitals/${hospitalId}`
    ),


  hospitalDoctors: (hospitalId) =>
    call(
      `/hospitals/${hospitalId}/doctors`
    ),


  doctorSlots: (
    hospitalId,
    doctorId
  ) =>
    call(
      `/hospitals/${hospitalId}/doctors/${doctorId}/slots`
    ),


  setDoctorCalendar: (
    hospitalId,
    doctorId,
    entries
  ) =>
    call(
      `/hospitals/${hospitalId}/doctors/${doctorId}/calendar`,
      {
        method: 'POST',
        body: entries,
      }
    ),


  blockDoctorTime: (
    hospitalId,
    doctorId,
    body
  ) =>
    call(
      `/hospitals/${hospitalId}/doctors/${doctorId}/block`,
      {
        method: 'POST',
        body,
      }
    ),


  createQuestionnaire: (
    hospitalId,
    body
  ) =>
    call(
      `/hospitals/${hospitalId}/questionnaires`,
      {
        method: 'POST',
        body,
      }
    ),


  // ==================================================
  // ADMIN
  // ==================================================

  overview: () =>
    call('/admin/overview'),


  pendingHospitals: () =>
    call('/admin/hospitals/pending'),


  approve: (id) =>
    call(
      `/admin/hospitals/${id}/approve`,
      {
        method: 'POST',
      }
    ),


  reject: (id) =>
    call(
      `/admin/hospitals/${id}/reject`,
      {
        method: 'POST',
      }
    ),


  adminAppointments: () =>
    call('/admin/appointments'),


  integrationOps: () =>
    call('/admin/integration-operations'),


  reconciliations: () =>
    call('/admin/reconciliations'),


  resolveRecon: (id) =>
    call(
      `/admin/reconciliations/${id}/resolve`,
      {
        method: 'POST',
      }
    ),


  auditLog: () =>
    call('/admin/audit'),


  trace: (correlationId) =>
    call(
      `/admin/trace/${correlationId}`
    ),


  runWorkflows: () =>
    call(
      '/admin/workflows/tick',
      {
        method: 'POST',
      }
    ),
}


// ====================================================
// MOCK EHR FAULT CONTROL
// ====================================================

export async function setFault(
  mode,
  remaining = 1
) {

  const res = await fetch(
    `${EHR_URL}/admin/fault`,
    {
      method: 'POST',

      headers: {
        'Content-Type': 'application/json',
      },

      body: JSON.stringify({
        mode,
        remaining,
      }),
    }
  )

  return res.json()
}