import { createBrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";

function Root() {
  return (
    <AuthProvider>
      <App />
    </AuthProvider>
  );
}

export const router = createBrowserRouter([
  { path: "*", element: <Root /> },
]);
