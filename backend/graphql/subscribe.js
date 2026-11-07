/**
 * Author: Trịnh Thiên Quang
 * Resolver giới hạn subscription theo người dùng Cognito và phiên hội thoại.
 */

import { extensions, util } from '@aws-appsync/utils';

// Attach to Subscription.onActionResult with a NONE data source.
export function request(ctx) {
  if (!ctx.identity || !ctx.identity.sub) {
    util.unauthorized();
  }
  return { payload: null };
}
export function response(ctx) {
  extensions.setSubscriptionFilter(util.transform.toSubscriptionFilter({
    user_id: { eq: ctx.identity.sub },
    session_id: { eq: ctx.args.session_id },
  }));
  return null;
}
