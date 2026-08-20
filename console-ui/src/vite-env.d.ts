/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_KEYCLOAK_URL: string;
  readonly VITE_KEYCLOAK_REALM: string;
  readonly VITE_OIDC_CLIENT_ID: string;
  readonly VITE_AUTH_DEV_MODE: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
