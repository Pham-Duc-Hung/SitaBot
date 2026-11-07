/**
 * Author: Trịnh Thiên Quang
 * Resolver phát kết quả tác vụ qua AppSync với quyền IAM.
 */

// Attach to Mutation.publishActionResult with a NONE data source; IAM-only field.
export function request(ctx) {
  return { payload: ctx.args.event };
}
export function response(ctx) {
  return ctx.result;
}
