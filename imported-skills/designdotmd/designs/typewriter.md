---
version: alpha
name: Typewriter
description: Ribbon black on manila, nothing else.
colors:
  primary: "#1A0F00"
  secondary: "#6B5B3E"
  tertiary: "#A8271E"
  neutral: "#EDDFB6"
  surface: "#F5EAC8"
  on-primary: "#F5EAC8"
typography:
  display:
    fontFamily: Special Elite
    fontSize: 3.75rem
    fontWeight: 400
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Special Elite
    fontSize: 2.25rem
    fontWeight: 400
  body:
    fontFamily: Courier Prime
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Courier Prime
    fontSize: 0.75rem
    letterSpacing: "0.04em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

A writer's tool. Monospaced throughout, manila paper background, single red for edits. Every word carries weight.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A0F00`):** Headlines and core text.
- **Secondary (`#6B5B3E`):** Borders, captions, and metadata.
- **Tertiary (`#A8271E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EDDFB6`):** The page foundation.

## Typography

- **display:** Special Elite 3.75rem
- **h1:** Special Elite 2.25rem
- **body:** Courier Prime 1rem
- **label:** Courier Prime 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
