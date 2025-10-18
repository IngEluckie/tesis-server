// script.js — Login + persistencia de JWT y perfil en localStorage + redirección con querystring

(() => {
  const form = document.querySelector(".login-form");
  const errorBox = document.getElementById("login-error");

  // ----- Config -----
  const ENDPOINT_LOGIN = "/auth/login";
  const PROFILE_CANDIDATES = ["/auth/me", "/auth/user", "/auth/profile"];

  // A dónde redirigir después del login
  const FRONT_REDIRECT = "http://127.0.0.1:3000/";
  // URL del backend (para pasarla como browser_url)
  const BROWSER_URL = "http://127.0.0.1:8000";

  // Helpers -----------------------------

  /** Muestra un mensaje de error accesible */
  function showError(msg) {
    if (errorBox) {
      errorBox.textContent = msg || "Error al iniciar sesión.";
    } else {
      alert(msg || "Error al iniciar sesión.");
    }
  }

  /** Limpia el error visible */
  function clearError() {
    if (errorBox) errorBox.textContent = "";
  }

  /** Extrae el token desde respuestas comunes */
  function pickToken(data) {
    // backends típicos: {access_token, token_type}, {jwt}, {token}
    return data?.access_token || data?.jwt || data?.token || null;
  }

  /** Guarda en localStorage de forma segura */
  function saveSession({ jwt, user }) {
    try {
      if (jwt) localStorage.setItem("jwt", jwt);
      if (user) localStorage.setItem("userdata", JSON.stringify(user));
    } catch (e) {
      console.warn("No se pudo persistir en localStorage:", e);
    }
  }

  /** Obtiene el perfil intentando varias rutas conocidas */
  async function fetchUserProfile(jwt) {
    for (const path of PROFILE_CANDIDATES) {
      try {
        const res = await fetch(path, {
          headers: {
            Authorization: `Bearer ${jwt}`,
            Accept: "application/json",
          },
        });
        if (res.ok) {
          const data = await res.json();
          // Normaliza campos esperados
          const user = {
            user_id: data.user_id ?? data.id ?? data.userId ?? null,
            username: data.username ?? data.user ?? data.name ?? null,
            email: data.email ?? null,
            ...data, // conserva el resto
          };
          return user;
        }
      } catch {
        // probar siguiente candidato
      }
    }
    return null;
  }

  /** Redirección post-login con los parámetros solicitados */
  function redirectWithParams({ jwt, user }) {
    let userString = "";
    try {
      userString = JSON.stringify(user ?? {});
    } catch {
      userString = "{}";
    }

    const qs = new URLSearchParams({
      jwt: jwt,
      browser_url: BROWSER_URL,
      userdata: userString,
    }).toString();

    // Redirección
    window.location.href = `${FRONT_REDIRECT}?${qs}`;
  }

  // Controlador de submit ----------------

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      clearError();

      // Empaqueta como x-www-form-urlencoded (requerido por OAuth2PasswordRequestForm)
      const fd = new FormData(form);
      const body = new URLSearchParams(fd);

      try {
        const res = await fetch(ENDPOINT_LOGIN, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            Accept: "application/json",
          },
          body,
        });

        if (!res.ok) {
          let message = "Credenciales inválidas.";
          try {
            const err = await res.json();
            message = err?.detail || message;
          } catch {}
          showError(message);
          return;
        }

        const data = await res.json();
        const token = pickToken(data);

        if (!token) {
          showError("No se recibió el token de acceso.");
          return;
        }

        // Guarda sólo el JWT por ahora
        saveSession({ jwt: token });

        // Intenta obtener y guardar el perfil
        const profile = await fetchUserProfile(token);
        if (profile) {
          saveSession({ jwt: token, user: profile });
        }

        // Redirige con parámetros en la URL (usa el perfil si existe; si no, objeto vacío)
        redirectWithParams({ jwt: token, user: profile || {} });
      } catch (err) {
        showError("No se pudo conectar con el servidor.");
        console.error(err);
      }
    });
  }
})();
