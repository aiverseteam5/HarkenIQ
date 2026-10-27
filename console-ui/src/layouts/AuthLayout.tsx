import { Outlet } from "react-router-dom";
import { BrandLockup } from "../components/BrandMark";

/** The sign-in surface. Its lockup is HarkenIQ Console (D4, A35.2); the
 *  mark is decoration, the words are the name. */
export default function AuthLayout() {
  return (
    <div className="auth-layout">
      <div className="auth-layout__brand">
        <BrandLockup size={24} />
      </div>
      <div className="auth-layout__card">
        <Outlet />
      </div>
    </div>
  );
}
