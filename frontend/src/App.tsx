import { Navigate, Route, Routes } from "react-router-dom";
import ProtectedRoute from "./auth/ProtectedRoute";
import AppLayout from "./components/AppLayout";
import LoginPage from "./pages/LoginPage";
import ProjectsPage from "./pages/ProjectsPage";
import VideosPage from "./pages/VideosPage";
import AnnotatePage from "./pages/AnnotatePage";
import ReviewPage from "./pages/ReviewPage";
import ClipsPage from "./pages/ClipsPage";
import ExportPage from "./pages/ExportPage";
import AdminPage from "./pages/AdminPage";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Navigate to="/projects" replace />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId/videos" element={<VideosPage />} />
          <Route path="/projects/:projectId/review" element={<ReviewPage />} />
          <Route path="/projects/:projectId/clips" element={<ClipsPage />} />
          <Route path="/projects/:projectId/export" element={<ExportPage />} />
          <Route path="/projects/:projectId/admin" element={<AdminPage />} />
          <Route path="/projects/:projectId/annotate/:videoId" element={<AnnotatePage />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
