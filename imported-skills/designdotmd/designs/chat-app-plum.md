---
version: alpha
name: Chat App Plum
description: Messaging: plum bubbles, chat-pop, read receipts.
colors:
  primary: "#23132E"
  secondary: "#8672A0"
  tertiary: "#7B44C7"
  neutral: "#F4EFFA"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 1.9rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A chat-app system: plum primary bubbles, soft neutral, read-state micro-type.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#23132E`):** Headlines and core text.
- **Secondary (`#8672A0`):** Borders, captions, and metadata.
- **Tertiary (`#7B44C7`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4EFFA`):** The page foundation.

## Typography

- **display:** Inter 3.5rem
- **h1:** Inter 1.9rem
- **body:** Inter 0.95rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
